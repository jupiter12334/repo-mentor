"""后台运行固定代码快照上的检查，并保存可用于验收的版本化证据。"""

import difflib
import fnmatch
import hashlib
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from repomentor.architecture import (
    ArchitectureMemory,
    architecture_hash,
    architecture_rule,
    check_architecture,
)
from repomentor.checks import check_rules
from repomentor.models import Rule
from repomentor.reviewer import REVIEW_VERSION, Reviewer
from repomentor.selection import digest
from repomentor.store import Store
from repomentor.workspace import fingerprint, git, git_names, has_head, tracked_files

VALIDATION_VERSION = 6


def capture_baseline(store: Store, run: dict):
    """对原地任务冻结起点，新增文件和 diff 不依赖 Git 或干净的 HEAD。"""
    root = Path(run["workspace"])
    base = store.directory / "baselines" / run["id"]
    base.mkdir(parents=True, exist_ok=True)
    files = tracked_files(root)
    before = fingerprint(root)
    for path in files:
        target = base / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    names = [p.relative_to(root).as_posix() for p in files]
    if before != fingerprint(root) or before != snapshot_fingerprint(base, names):
        raise ValueError("冻结任务起点时文件变化，请重新开始")
    run["baseline"] = {"directory": str(base), "files": names}
    store.put("run", run["id"], run)


def changes(root: Path, run: dict) -> tuple[list[str], list[str], str]:
    if run.get("baseline"):
        baseline = run["baseline"]
        old_names = set(baseline["files"])
        names = {p.relative_to(root).as_posix() for p in tracked_files(root)}
        changed, chunks = [], []
        for name in sorted(old_names | names):
            old_path = Path(baseline["directory"]) / name
            new_path = root / name
            old = old_path.read_bytes() if name in old_names else b""
            new = new_path.read_bytes() if name in names else b""
            if old != new or (name in old_names) != (name in names):
                changed.append(name)
                chunks.extend(
                    difflib.unified_diff(
                        old.decode("utf-8", errors="replace").splitlines(True),
                        new.decode("utf-8", errors="replace").splitlines(True),
                        fromfile="a/" + name,
                        tofile="b/" + name,
                    )
                )
        return changed, sorted(names - old_names), "".join(chunks)
    if has_head(root):
        changed = git_names(root, "diff", "--relative", "--name-only", "HEAD", "--", ".")
        new = git_names(root, "ls-files", "--others", "--exclude-standard", "--", ".")
        created = git_names(
            root,
            "diff",
            "--relative",
            "--name-only",
            "--diff-filter=A",
            "--no-renames",
            "HEAD",
            "--",
            ".",
        )
        return (
            changed + new,
            sorted(set(created + new)),
            git(root, "diff", "--relative", "HEAD", "--", "."),
        )
    names = [p.relative_to(root).as_posix() for p in tracked_files(root)]
    return names, names, ""


def rule_fingerprint(rules: list[Rule]) -> str:
    return digest([r.to_dict() for r in rules])


def snapshot_fingerprint(root: Path, names: list[str]) -> str:
    value = hashlib.sha256()
    for name in names:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("验证快照文件被删除或替换")
        value.update(name.encode() + b"\0")
        value.update(path.read_bytes())
        value.update(b"\0")
    return value.hexdigest()


class BackgroundChecks:
    def __init__(self, store: Store, reviewer: Reviewer | None = None):
        self.store = store
        self.reviewer = reviewer
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.futures = {}
        self.processes = {}
        self.lock = threading.Lock()

    def start(self, run: dict, rules: list[Rule]) -> str:
        root = Path(run["workspace"])
        id = "V" + uuid.uuid4().hex[:10]
        snapshot = self.store.directory / "snapshots" / id
        snapshot.mkdir(parents=True)
        before = fingerprint(root)
        # 与复制操作绑定的 diff 用于独立评审；新文件正文由快照提供。
        changed, created, diff = changes(root, run)
        architecture = ArchitectureMemory(self.store).get(run["repo_id"])
        # 验收本次修改与显式指定的目标，避免让旧目录的历史违规阻塞无关任务。
        targets = sorted(set(changed + run.get("target_paths", run.get("paths", []))))
        if not targets:
            targets = [
                p.relative_to(root).as_posix()
                for p in tracked_files(root)
                if any(
                    fnmatch.fnmatch(p.relative_to(root).as_posix(), pattern)
                    for pattern in run["allowed_paths"]
                )
            ]
        scoped_rules = []
        for rule in rules:
            matched = [
                p for p in targets if any(fnmatch.fnmatch(p, pattern) for pattern in rule.paths)
            ]
            if matched:
                scoped_rules.append(replace(rule, paths=matched))
        files = tracked_files(root)
        names = [path.relative_to(root).as_posix() for path in files]
        created = sorted(set(created) & set(names))
        arch_rule = architecture_rule(architecture, [p for p in targets if p in names])
        if arch_rule:
            scoped_rules.append(arch_rule)
        for path in files:
            target = snapshot / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        if before != fingerprint(root) or before != snapshot_fingerprint(snapshot, names):
            raise ValueError("创建验证快照时代码发生变化，请重新验证")
        record = {
            "id": id,
            "run_id": run["id"],
            "status": "running",
            "stage": "等待后台执行",
            "passed": False,
            "code_hash": before,
            "rule_hash": rule_fingerprint(rules),
            "architecture_hash": architecture_hash(architecture),
            "architecture": architecture,
            "created_files": created,
            "profile_hash": digest(run["profile"]),
            "validation_version": VALIDATION_VERSION,
            "scope_hash": digest(
                {
                    "allow": run["allowed_paths"],
                    "targets": run.get("target_paths", run.get("paths", [])),
                }
            ),
            "task_hash": digest(run["request"]),
            "baseline_hash": digest(run.get("baseline")),
            "files": names,
            "targets": targets,
            "snapshot": str(snapshot),
            "commands": [],
            "findings": [],
            "unchecked_rules": [],
            "error": None,
            "review": None,
            "mcp_evidence": [
                e["data"] for e in self.store.events(run["id"]) if e["type"] == "mcp_called"
            ][-20:],
        }
        self.store.put("verification", id, record)
        self.store.event(run["id"], "verification_started", {"id": id, "code_hash": before})
        self.futures[id] = self.pool.submit(
            self._worker,
            record,
            run,
            scoped_rules,
            diff,
            sorted(set(targets + run.get("paths", []))),
        )
        return id

    def _worker(self, record: dict, run: dict, rules: list[Rule], diff: str, paths: list[str]):
        snapshot = Path(record["snapshot"])
        profile = run["profile"]
        try:
            self._stage(record, "静态规范与架构检查")
            if profile.get("runner") == "compileall" and not any(
                p.endswith(".py") for p in record["files"]
            ):
                raise ValueError(
                    "当前目录没有 Python 源码，不能用编译检查验收；请配置该项目的实际检查命令"
                )
            findings, unchecked = check_rules(snapshot, rules)
            findings.extend(
                check_architecture(
                    snapshot, record["architecture"], record["targets"], record["created_files"]
                )
            )
            record.update(findings=findings, unchecked_rules=unchecked)
            review_policy = profile.get("review", {})
            if any(r.mandatory and r.id in unchecked for r in rules) and not review_policy.get(
                "enabled"
            ):
                raise ValueError("必选规范缺少机器检查器，不能自动声明验收通过")
            for index, argv in enumerate(profile["commands"]):
                self._stage(record, f"检查命令 {index + 1}/{len(profile['commands'])}")
                if not argv or not all(isinstance(a, str) for a in argv):
                    raise ValueError("检查命令必须是非空参数数组")
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if not any(
                        word in k
                        for word in (
                            "ANTHROPIC",
                            "GITHUB",
                            "GH_TOKEN",
                            "GITLAB",
                            "API_KEY",
                            "TOKEN",
                            "SECRET",
                            "PASSWORD",
                        )
                    )
                }
                env.update(profile.get("env", {}))
                env["PYTHONPATH"] = os.pathsep.join(
                    str(snapshot / p) for p in profile.get("pythonpath", ["src"])
                )
                log = self.store.directory / "snapshots" / f"{record['id']}-{index}.log"
                start = time.perf_counter()
                with log.open("w", encoding="utf-8") as output:
                    process = subprocess.Popen(
                        argv,
                        cwd=snapshot,
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    with self.lock:
                        self.processes[record["id"]] = process
                    try:
                        code = process.wait(timeout=profile.get("timeout", 180))
                    except subprocess.TimeoutExpired:
                        self._terminate(process)
                        raise ValueError("检查命令超时") from None
                    finally:
                        with self.lock:
                            self.processes.pop(record["id"], None)
                record["commands"].append(
                    {
                        "argv": argv,
                        "exit_code": code,
                        "log": str(log),
                        "seconds": round(time.perf_counter() - start, 3),
                    }
                )
                if code:
                    break
            if snapshot_fingerprint(snapshot, record["files"]) != record["code_hash"]:
                raise ValueError("测试命令修改了验证源码，不能用于原代码验收")
            machine_passed = (
                not findings
                and bool(record["commands"])
                and len(record["commands"]) == len(profile["commands"])
                and all(c["exit_code"] == 0 for c in record["commands"])
            )
            # 明确的执行失败先交给编码器修复，避免为已知失败重复支付语义评审用量。
            if review_policy.get("enabled") and machine_passed:
                self._stage(record, "独立规范评审")
                if (
                    self.reviewer is None
                    or self.reviewer.model.name != review_policy["model"]
                    or review_policy["version"] != REVIEW_VERSION
                    or review_policy.get("scope", self.reviewer.scope) != self.reviewer.scope
                ):
                    raise ValueError("Reviewer 配置缺失或版本变化，请重新建立运行")
                record["review"] = self.reviewer.review(
                    snapshot,
                    run["request"],
                    rules,
                    paths,
                    diff,
                    review_policy.get("max_chars", 80000),
                    file_names=record["files"],
                    execution_evidence=record["mcp_evidence"],
                    architecture=record["architecture"],
                    created_files=record["created_files"],
                    change_paths=record["targets"],
                    deleted_files={
                        name: (Path(run["baseline"]["directory"]) / name).read_text(
                            encoding="utf-8"
                        )
                        for name in record["targets"]
                        if run.get("baseline")
                        and name not in record["files"]
                        and name in run["baseline"]["files"]
                    },
                )
                reviewed = {r["rule_id"] for r in record["review"]["results"]}
                if any(r.mandatory and r.id in unchecked and r.id not in reviewed for r in rules):
                    raise ValueError("必选规范未被机器检查或 Reviewer 覆盖")
            record["passed"] = machine_passed and (
                not review_policy.get("enabled")
                or bool(record["review"] and record["review"]["passed"])
            )
            record["status"] = "completed"
            record["stage"] = "完成"
        except Exception as error:
            record.update(
                status="failed",
                stage="失败",
                error=(
                    str(error)
                    if isinstance(error, ValueError)
                    else f"{type(error).__name__}：验证执行或语义评审失败，请重新调用 verify；"
                    "若持续失败，应拆分任务或检查接口配置。"
                ),
                passed=False,
            )
        self.store.put("verification", record["id"], record)
        self.store.event(
            record["run_id"],
            "verification_finished",
            {"id": record["id"], "passed": record["passed"], "status": record["status"]},
        )

    def _stage(self, record: dict, stage: str):
        """后台状态由实际执行步骤更新，终端查询无需猜测进度。"""
        record["stage"] = stage
        self.store.put("verification", record["id"], record)
        self.store.event(
            record["run_id"], "verification_stage", {"id": record["id"], "stage": stage}
        )

    @staticmethod
    def _terminate(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        except ProcessLookupError:
            pass

    def wait(self, id: str, seconds: float = 10) -> dict:
        future = self.futures.get(id)
        if future is not None:
            try:
                future.result(timeout=min(max(seconds, 0), 10))
            except TimeoutError:
                pass
        return self.store.get("verification", id)

    def close(self):
        with self.lock:
            processes = list(self.processes.values())
        for process in processes:
            self._terminate(process)
        self.pool.shutdown(wait=True, cancel_futures=True)


def assess(store: Store, run: dict) -> dict:
    rules = store.rules(run["repo_id"])
    records = [v for v in store.list("verification") if v["run_id"] == run["id"]]
    current = fingerprint(Path(run["workspace"]))
    current_architecture = architecture_hash(ArchitectureMemory(store).get(run["repo_id"]))
    valid = [
        v
        for v in records
        if v["code_hash"] == current
        and v.get("validation_version", 1) == VALIDATION_VERSION
        and v["rule_hash"] == rule_fingerprint(rules)
        and v.get("architecture_hash") == current_architecture
        and v.get("baseline_hash") == digest(run.get("baseline"))
        and v["profile_hash"] == digest(run["profile"])
        and ("task_hash" not in v or v["task_hash"] == digest(run["request"]))
        and v.get("scope_hash")
        == digest(
            {
                "allow": run["allowed_paths"],
                "targets": run.get("target_paths", run.get("paths", [])),
            }
        )
    ]
    latest = valid[-1] if valid else None
    external = run.get("external_writes", [])
    evidence = bool(
        latest and latest["status"] == "completed" and latest["passed"] and not external
    )
    pending = [t["id"] for t in store.tasks(run["id"]) if t["status"] != "done"]
    return {
        "achieved": evidence and not pending,
        "evidence_valid": evidence,
        "pending_tasks": pending,
        "verification_id": latest["id"] if latest else None,
        "external_writes": external,
        "reason": (
            "验收条件已满足"
            if evidence and not pending
            else "仍有未完成任务"
            if evidence
            else "目录外写入尚未纳入本项目验收，不能自动声明整体完成"
            if external
            else "缺少当前代码和规范版本上的通过记录"
        ),
    }
