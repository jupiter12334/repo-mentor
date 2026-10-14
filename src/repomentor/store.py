"""使用 SQLite 保存规范版本、任务、会话和执行证据。"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from repomentor.models import Rule


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT, id TEXT, data TEXT, PRIMARY KEY (kind, id)
                );
                CREATE TABLE IF NOT EXISTS rules (
                    repo_id TEXT, id TEXT, version INTEGER, status TEXT, data TEXT,
                    PRIMARY KEY (repo_id, id, version)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT, type TEXT, data TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS architectures (
                    repo_id TEXT, version INTEGER, status TEXT, data TEXT,
                    PRIMARY KEY (repo_id, version)
                );
                CREATE TABLE IF NOT EXISTS architecture_nodes (
                    repo_id TEXT, version INTEGER, path TEXT, data TEXT,
                    PRIMARY KEY (repo_id, version, path)
                );
            """)

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=20)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def put(self, kind: str, id: str, data: dict):
        with self.connect() as db:
            db.execute(
                "INSERT INTO objects VALUES (?, ?, ?) "
                "ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (kind, id, json.dumps(data, ensure_ascii=False)),
            )

    def put_once(self, kind: str, id: str, data: dict) -> dict:
        # 重复导入同一评审时保留原工作流；插入和读取在同一事务内。
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO objects VALUES(?,?,?)",
                (kind, id, json.dumps(data, ensure_ascii=False)),
            )
            row = db.execute(
                "SELECT data FROM objects WHERE kind=? AND id=?", (kind, id)
            ).fetchone()
            return json.loads(row["data"])

    def get(self, kind: str, id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM objects WHERE kind=? AND id=?", (kind, id)
            ).fetchone()
        if row is None:
            raise ValueError(f"找不到 {kind}：{id}")
        return json.loads(row["data"])

    def list(self, kind: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM objects WHERE kind=? ORDER BY rowid", (kind,))
            return [json.loads(row["data"]) for row in rows]

    def event(self, run_id: str, type: str, data: dict):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(run_id,type,data) VALUES(?,?,?)",
                (run_id, type, json.dumps(data, ensure_ascii=False)),
            )

    def events(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE run_id=? ORDER BY sequence", (run_id,))
            return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def propose(
        self,
        repo_id: str,
        requirement: str,
        *,
        id: str | None = None,
        paths: list[str] | None = None,
        learning_id: str | None = None,
        **kwargs,
    ) -> Rule:
        if not requirement.strip():
            raise ValueError("规范正文不能为空")
        if id == "Architecture":
            raise ValueError("Architecture 是架构验收保留 ID，请使用架构管理工具")
        if kwargs.get("status", "candidate") != "candidate":
            raise ValueError("新规范只能是待确认候选")
        if not paths:
            paths = ["**"]
        if any(p.startswith("/") or ".." in Path(p).parts for p in paths):
            raise ValueError("规范作用域必须是仓库内的相对路径或通配符")
        kind = kwargs.get("kind", "requirement")
        if kwargs.get("review_backend", "llm") not in {"llm", "jev"}:
            raise ValueError("规范评审后端必须是 llm 或 jev")
        if kind not in {"requirement", "exception"}:
            raise ValueError("规范类型必须是 requirement 或 exception")
        if kind == "requirement" and (
            kwargs.get("parent_id") or kwargs.get("parent_version") is not None
        ):
            raise ValueError("普通规范不能绑定例外父规则")
        if kind == "exception":
            if kwargs.get("review_backend", "llm") != "llm":
                raise ValueError("文件例外由程序处理，不能指定 Jev 评审")
            parents = [r for r in self.rules(repo_id) if r.id == kwargs.get("parent_id")]
            if len(parents) != 1 or parents[0].kind != "requirement":
                raise ValueError("例外必须绑定当前仓库的一条生效规范")
            parent = parents[0]
            if id == parent.id:
                raise ValueError("例外不能复用父规范的 ID")
            if kwargs.get("parent_version") != parent.version:
                raise ValueError("例外的父规范版本已变化")
            if kwargs.get("condition") or kwargs.get("checker") or kwargs.get("mandatory"):
                raise ValueError("当前例外仅支持无条件的具体文件范围，不能携带检查器或必选标记")
            if any(
                any(c in p for c in "*?[")
                or not any(fnmatch.fnmatch(p, pattern) for pattern in parent.paths)
                for p in paths
            ):
                raise ValueError("例外必须列出父规范范围内的具体文件，不能使用通配符")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if learning_id:
                learning_key = repo_id + ":" + learning_id
                existing = db.execute(
                    "SELECT data FROM objects WHERE kind='learning_candidate' AND id=?",
                    (learning_key,),
                ).fetchone()
                if existing:
                    return Rule(**json.loads(existing["data"]))
            id = id or "R" + uuid.uuid4().hex[:8]
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM rules WHERE repo_id=? AND id=?",
                (repo_id, id),
            ).fetchone()[0]
            rule = Rule(id, repo_id, version, requirement.strip(), paths=paths, **kwargs)
            db.execute(
                "INSERT INTO rules VALUES(?,?,?,?,?)",
                (repo_id, id, version, rule.status, json.dumps(rule.to_dict(), ensure_ascii=False)),
            )
            if learning_id:
                db.execute(
                    "INSERT INTO objects VALUES('learning_candidate',?,?)",
                    (learning_key, json.dumps(rule.to_dict(), ensure_ascii=False)),
                )
        return rule

    def confirm(
        self,
        repo_id: str,
        id: str,
        version: int | None = None,
        *,
        replace: str | None = None,
        replace_version: int | None = None,
        expected_rules: set[tuple[str, int]] | None = None,
    ) -> Rule:
        candidates = [r for r in self.rules(repo_id, active_only=False) if r.id == id]
        latest = max(candidates, key=lambda r: r.version) if candidates else None
        checked_provenance = None
        if latest and latest.status == "candidate" and latest.provenance:
            if version != latest.version:
                raise ValueError("确认评审候选需要明确指定当前版本")
            from repomentor.reviews import validate_provenance

            validate_provenance(self, latest.provenance)
            checked_provenance = latest.provenance
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if expected_rules is not None:
                current_rules = {
                    (r["id"], r["version"])
                    for r in db.execute(
                        "SELECT id,version FROM rules WHERE repo_id=? AND status='active'",
                        (repo_id,),
                    )
                }
                if current_rules != expected_rules:
                    raise ValueError("生效规范库已变化，请重新起草候选并确认")
            row = db.execute(
                "SELECT * FROM rules WHERE repo_id=? AND id=? ORDER BY version DESC LIMIT 1",
                (repo_id, id),
            ).fetchone()
            if row is None or (version is not None and row["version"] != version):
                raise ValueError("规范不存在或版本已变化，请重新确认")
            if row["status"] not in {"candidate", "active"}:
                raise ValueError("只能确认候选规范")
            data = json.loads(row["data"])
            if (
                row["status"] == "candidate"
                and data.get("provenance")
                and data["provenance"] != checked_provenance
            ):
                raise ValueError("候选来源发生变化，请重新确认")
            if row["status"] == "active" and not replace:
                return Rule(**{**data, "status": "active"})
            if data.get("kind") == "exception":
                parent = db.execute(
                    "SELECT version FROM rules WHERE repo_id=? AND id=? AND status='active'",
                    (repo_id, data["parent_id"]),
                ).fetchone()
                if parent is None or parent["version"] != data["parent_version"]:
                    raise ValueError("例外的父规范版本已变化，请重新提出")
            for relation in data.get("relations", []):
                current = db.execute(
                    "SELECT version FROM rules WHERE repo_id=? AND id=? AND status='active'",
                    (repo_id, relation["id"]),
                ).fetchone()
                if current is None or current["version"] != relation["version"]:
                    raise ValueError("关联规范版本已变化，请重新提炼反馈")
                if (
                    relation["relation"] == "conflict"
                    and replace != relation["id"]
                    and id != relation["id"]
                ):
                    raise ValueError("候选规范有冲突，需要明确指定 --replace 和 --replace-version")
            if replace:
                target = db.execute(
                    "SELECT version,data FROM rules WHERE repo_id=? AND id=? AND status='active'",
                    (repo_id, replace),
                ).fetchone()
                if (
                    replace == id
                    or replace_version is None
                    or target is None
                    or target["version"] != replace_version
                    or json.loads(target["data"]).get("kind", "requirement") != "requirement"
                    or data.get("kind", "requirement") != "requirement"
                ):
                    raise ValueError("替代目标不存在或版本不匹配，不能替代例外")
                db.execute(
                    "UPDATE rules SET status='superseded' WHERE repo_id=? AND id=?",
                    (repo_id, replace),
                )
            # 父规范修订后，旧例外不能继续豁免新规则。
            for parent_id in {id, replace} - {None}:
                for child in db.execute(
                    "SELECT id,version,data FROM rules WHERE repo_id=? AND status='active'",
                    (repo_id,),
                ).fetchall():
                    child_data = json.loads(child["data"])
                    if child_data.get("parent_id") == parent_id:
                        db.execute(
                            "UPDATE rules SET status='expired' WHERE repo_id=? AND id=? AND version=?",
                            (repo_id, child["id"], child["version"]),
                        )
            # 在同一事务中替代旧版本，保证一个规则只存在一个生效版本。
            db.execute(
                "UPDATE rules SET status='superseded' WHERE repo_id=? AND id=?", (repo_id, id)
            )
            data["status"] = "active"
            db.execute(
                "UPDATE rules SET status='active',data=? WHERE repo_id=? AND id=? AND version=?",
                (json.dumps(data, ensure_ascii=False), repo_id, id, row["version"]),
            )
        return Rule(**data)

    def rules(self, repo_id: str, active_only: bool = True) -> list[Rule]:
        with self.connect() as db:
            sql = "SELECT data,status FROM rules WHERE repo_id=?"
            if active_only:
                sql += " AND status='active'"
            rows = db.execute(sql + " ORDER BY id,version", (repo_id,))
            return [Rule(**{**json.loads(r["data"]), "status": r["status"]}) for r in rows]

    def task(
        self,
        run_id: str,
        subject: str,
        depends: list[str] | None = None,
        kind: str = "implementation",
    ) -> dict:
        depends = depends or []
        for dependency in depends:
            if self.get("task", dependency)["run_id"] != run_id:
                raise ValueError("依赖必须属于同一次运行")
        task = {
            "id": "T" + uuid.uuid4().hex[:8],
            "run_id": run_id,
            "subject": subject,
            "depends": depends,
            "kind": kind,
            "status": "pending",
        }
        self.put("task", task["id"], task)
        return task

    def tasks(self, run_id: str) -> list[dict]:
        return [t for t in self.list("task") if t["run_id"] == run_id]

    def update_task(self, run_id: str, id: str, status: str, verified: bool = False) -> dict:
        if status not in {"pending", "running", "done"}:
            raise ValueError("任务状态必须是 pending、running 或 done")
        with self.lock:
            task = self.get("task", id)
            if task["run_id"] != run_id:
                raise ValueError("不能更新其他运行的任务")
            if status != "pending" and any(
                self.get("task", d)["status"] != "done" for d in task["depends"]
            ):
                raise ValueError("前置任务尚未完成")
            if status == "done" and task["kind"] == "verification" and not verified:
                raise ValueError("验收任务需要当前代码与规范版本上的有效执行证据")
            task["status"] = status
            self.put("task", id, task)
        return task
