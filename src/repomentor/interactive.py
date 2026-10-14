"""统一自然语言入口：管理记忆、发起编码、恢复任务，默认原地使用启动目录。"""

import fcntl
import json
import sys
from pathlib import Path

from jsonschema import ValidationError, validate

from repomentor.agent import TOOLS, Agent, new_run, read_owned_output, tool, verification_result
from repomentor.bash import BashRunner
from repomentor.chat import CHAT_TOOLS, INSTRUCTIONS, RuleChat
from repomentor.file_actions import delete_regular_file, modify_text_file, single_delete_request
from repomentor.harness.context import COMPACTION_INSTRUCTIONS
from repomentor.harness.hooks import Hooks
from repomentor.permissions import PathPermissions, PermissionDenied
from repomentor.reviewer import review_policy
from repomentor.selection import RuleSelector
from repomentor.store import Store
from repomentor.verification import assess, capture_baseline, changes
from repomentor.workspace import repo_identity

UNIFIED_TOOLS = [
    *[t for t in CHAT_TOOLS if t["name"] not in {"read_file", "compact", "read_output"}],
    *[t for t in TOOLS if t["name"] != "get_architecture"],
    tool(
        "begin_coding_task",
        "仅在用户要求实际开发代码（实现功能、修改程序逻辑、修复 bug、编写测试）时登记任务。"
        "解释代码、讨论方案、文件整理、普通文本编辑、仅运行命令/测试均不调用此工具。"
        "调用后才按需加载项目架构与规范，当前对话继续编码，无子智能体。",
        {"request": {"type": "string", "minLength": 1, "maxLength": 12000}},
        ["request"],
    ),
    tool("resume_coding_task", "继续本会话最近未完成的编码任务，不需要开发者记运行 ID。"),
    tool("coding_status", "查看本会话最近编码任务、修改文件和真实验收状态。"),
    tool(
        "set_test_runner",
        "配置当前 Python 工程的验证方式；已有正式项目检查优先保留。",
        {"runner": {"type": "string", "enum": ["pytest", "unittest", "compileall"]}},
        ["runner"],
    ),
]
UNIFIED_INSTRUCTIONS = (
    "你是 RepoMentor 通用工具助手，在显示的当前工作目录工作，使用中文交流。"
    "直接根据本轮用户需求和对话上下文决定回答或调用哪个工具，不先调用意图分类器。"
    "默认处理普通请求；只有用户确实要求实际开发代码时才启动编码任务。"
    "解释代码、分析原因、讨论改法、给出示例、查看日志或目录、整理/重命名/删除文件、"
    "编写普通文档、仅运行命令或测试，都直接使用通用工具或回答，不登记编码任务，不自动修复失败。"
    "文件后缀是 .py、问题涉及代码、工作目录含工程、历史任务未完成，都不表示本轮要你写代码。"
    "用户说只分析/不要修改时不改文件；不明确是否要求实际修改时先询问。"
    "例如解释这个函数、把a.txt改名为b.txt、创建空的test.py、运行pytest只告诉我结果：普通处理。"
    "例如修复登录bug、给接口增加分页、修改函数实现、编写单元测试：实际开发代码。"
    "实际开发时先读取必要文件了解需求，在修改程序前调用 begin_coding_task，"
    "宿主会把适用规范和架构加入当前上下文；之后由你继续读写代码、推进任务并执行 verify。"
    "评审只附加在实际编码流程中。普通操作不复制整个目录，不跑项目验收，不生成验证文件。"
    "历史未完成任务只有在用户要求继续开发时才调用 resume_coding_task；查询状态不代表继续。"
    "普通请求不会自动加载项目架构；用户明确查询架构或规范时调用相应读取工具。"
    "用户可直接添加规范、登记架构、查询记忆，无需给出 CLI 命令。"
    "规范和架构复用草稿、展示、确认流程；不能擅自跳过确认。"
    "询问已有规范用 list_rules，架构用 get_architecture；不会根据模型记忆回答已保存内容。"
    "仅删除指定的单个文件是普通文件操作，直接调用 delete_file，不调用 begin_coding_task、"
    "不创建任务、不运行整个目录的测试，也不能创建额外验证文件。目标后缀是 .py 也不代表开发任务。"
    "request 完整保留用户需求，可以补充已讨论的实现细节；不能扩展无关任务。"
    "编码验收只根据 verify/coding_status 的真实 gate 报告。普通操作只报告工具的实际结果。"
    "文件工具允许工作目录内普通文件，目录外读写和查询会交给宿主询问；不能自己批准。"
    "开发者要求访问目录外文件时，照常调用 read_file/glob 触发宿主询问；"
    "不要因为路径在目录外就口头拒绝或要求搬进项目，权限必须交给宿主决定。"
    "没有完成编码工具调用不能声称修改了代码；当前只有编译检查时不能声称业务测试通过。"
    "bash 可用于检索、测试和构建，通过 permission_hook 的硬拒绝与参数检查后直接执行，"
    "不需要用户确认。文件工具的目录外权限询问不适用于 Bash 命令。"
    "通过 Bash 实现代码开发前必须先 begin_coding_task 或 resume_coding_task；"
    "单文件删除只检查指定目标是否还存在，不需要编码验收。"
    "编码任务中 Bash 成功不是最终验收，仍须 verify；普通命令结束即可报告其退出码和输出。"
    "调用工具前可用一句话说明接下来要做的事；工具和模型阶段会由终端显示。"
    "一次消息最多发起或恢复一个编码任务。临时实现需求不自动变成长期规范。\n"
)


def default_profile(root: Path) -> dict:
    interpreter = root / ".venv/bin/python"
    python = str(interpreter) if interpreter.exists() else sys.executable
    custom = root / ".repomentor/checks.json"
    if custom.is_file():
        profile = json.loads(custom.read_text(encoding="utf-8"))
        if not profile.get("commands") or not all(
            isinstance(c, list) and c and all(isinstance(a, str) for a in c)
            for c in profile["commands"]
        ):
            raise ValueError("checks.json 的 commands 必须是非空命令参数数组")
        profile["commands"] = [
            [a.replace("{python}", python) for a in c] for c in profile["commands"]
        ]
        return profile
    has_tests = (root / "tests").is_dir() or any(root.glob("test_*.py"))
    return {
        "commands": [[python, "-m", "pytest", "-q"]]
        if has_tests
        else [[python, "-m", "compileall", "-q", "."]],
        "pythonpath": [".", "src", "tests"],
        "timeout": 180,
        "runner": "pytest" if has_tests else "compileall",
    }


def current_project(store: Store, root: Path) -> dict:
    root = root.resolve()
    id = repo_identity(root)
    try:
        repo = store.get("repo", id)
        repo["path"] = str(root)
    except ValueError:
        repo = {"id": id, "path": str(root), "profile": default_profile(root)}
    store.put("repo", id, repo)
    return repo


class UnifiedChat(RuleChat):
    def __init__(
        self,
        store,
        repo,
        session_id,
        model,
        skills: Path,
        max_rounds=30,
        permissions=None,
    ):
        super().__init__(store, repo, session_id, model, max_rounds)
        self.tools, self.instructions = (
            UNIFIED_TOOLS,
            COMPACTION_INSTRUCTIONS + UNIFIED_INSTRUCTIONS + INSTRUCTIONS,
        )
        self.schemas = {t["name"]: t["input_schema"] for t in self.tools}
        self.report_no_rule_change = False
        self.skills = skills
        self.path_permissions = permissions or PathPermissions(
            self.root, event=lambda t, d: store.event(self.id, t, d)
        )
        self.runtime, self.run_lock, self.stop_blocks = None, None, 0
        self.file_request, self.file_outcome = None, None
        self.shell_result = None
        self.shell = BashRunner(
            self.root,
            store.directory / "archives" / self.id,
            event=lambda t, d: store.event(self.id, t, d),
        )
        self.hooks = Hooks()
        self.hooks.register("PreToolUse", self._permission)

    def _permission(self, block: dict):
        if self.runtime and block["name"] in {t["name"] for t in TOOLS}:
            return self.runtime.hooks.trigger("PreToolUse", block)
        if block["name"] == "bash":
            self.shell.check(block.get("input", {}))

    def _before_tool(self, block: dict):
        name, args = block["name"], block.get("input", {})
        if name not in self.schemas:
            raise ValueError("工具不在统一聊天白名单中")
        try:
            validate(args, self.schemas[name])
        except ValidationError:
            raise ValueError("工具参数不符合定义") from None
        if self.file_request:
            if self.file_outcome and not self.file_outcome["exists_after"]:
                raise PermissionDenied("指定文件操作已经完成，取消后续工具调用")
            if name not in {"delete_file", "bash", "read_file", "glob", "list_files"}:
                raise PermissionDenied(
                    "当前请求仅删除一个文件，请调用 delete_file；无需编码任务和项目测试"
                )
            if name == "delete_file":
                target = (self.root / args["path"]).resolve()
                expected = (self.root / Path(self.file_request).expanduser()).resolve()
                if target != expected:
                    raise PermissionDenied("本次请求只允许删除开发者指定的目标文件")
        blocked = self.hooks.trigger("PreToolUse", block)
        if blocked:
            raise PermissionDenied(blocked)

    def send(self, text: str) -> dict:
        self.stop_blocks = 0
        self.shell_result = None
        self.file_request, self.file_outcome = single_delete_request(text), None
        try:
            return super().send(text)
        except BaseException as error:
            if self.runtime:
                self.runtime.run.update(
                    status="interrupted"
                    if isinstance(error, (KeyboardInterrupt, SystemExit))
                    else "failed",
                    error=f"{type(error).__name__}：当前对话执行中断",
                )
            raise
        finally:
            try:
                if self.runtime:
                    try:
                        self.runtime.checks.close()
                        if self.runtime.mcp:
                            self.runtime.mcp.close()
                    finally:
                        self.runtime._save(self.session["messages"])
                        self.runtime = None
            finally:
                if self.run_lock:
                    self.run_lock.close()
                    self.run_lock = None

    def _architecture_facts(self):
        # 普通系统提示词仅保留候选版本绑定；完整架构通过工具或编码上下文按需读取。
        return {"architecture_pending": self.session.get("architecture_pending")}

    def _extra_facts(self):
        facts = {
            "working_directory": str(self.root),
            "last_coding_run": self.session.get("coding_run"),
            "coding_runtime_active": self.runtime is not None,
            "single_file_operation": {"action": "delete", "path": self.file_request}
            if self.file_request
            else None,
        }
        if self.runtime:
            for record in self.store.list("verification"):
                if (
                    record["run_id"] == self.runtime.run["id"]
                    and record["status"] != "running"
                    and record["id"] not in self.runtime.notified
                ):
                    self.session["messages"].append(
                        {
                            "role": "user",
                            "content": "[后台验证通知]\n"
                            + json.dumps(
                                self.runtime._verification(record["id"]), ensure_ascii=False
                            ),
                        }
                    )
                    self.runtime.notified.add(record["id"])
            self.runtime._save(self.session["messages"])
            facts.update(
                check_commands=self.repo["profile"].get("commands", []),
                coding_context=self.runtime._system(),
                tasks=self.store.tasks(self.runtime.run["id"]),
            )
        return facts

    def _after_tools(self, calls, results):
        if self.file_request and self.file_outcome and not self.runtime:
            outcome = self.file_outcome
            if not outcome["exists_after"]:
                prefix = (
                    "已删除："
                    if outcome["deleted"]
                    else "目标文件已不存在："
                    if outcome.get("resumed")
                    else "目标文件原本不存在："
                )
                return prefix + outcome["path"]
        return None

    def _on_stop(self):
        if not self.runtime:
            return True
        gate = self.runtime.hooks.trigger("Stop")
        self.runtime._event("goal_gate", gate)
        if gate["achieved"]:
            self.runtime.run.update(status="completed", gate=gate)
            return True
        self.stop_blocks += 1
        if self.stop_blocks >= 3:
            self.runtime.run.update(status="incomplete", gate=gate)
            return True
        self.session["messages"].append(
            {
                "role": "user",
                "content": "[验收尚未通过，继续当前任务]\n" + json.dumps(gate, ensure_ascii=False),
            }
        )
        return False

    def _last_run(self):
        id = self.session.get("coding_run")
        if not id:
            return None
        run = self.store.get("run", id)
        if (
            run["repo_id"] != self.repo["id"]
            or Path(run["workspace"]).resolve() != self.root.resolve()
        ):
            raise ValueError("不能读取另一工作目录的编码任务")
        return run

    def _status(self):
        if self.runtime:
            self.runtime._save(self.session["messages"])
        run = self._last_run()
        if not run:
            return {"status": "no_coding_task"}
        id = run["id"]
        legacy_file = single_delete_request(run.get("original_request") or run["request"])
        if legacy_file and not self.runtime:
            path = self.path_permissions.resolve("file_status", legacy_file, "检查文件是否存在")
            exists = path.exists() or path.is_symlink()
            return {
                "kind": "file_operation",
                "run_id": id,
                "operation": "delete_file",
                "path": str(path),
                "exists_after": exists,
                "message": ("目标文件仍然存在：" if exists else "目标文件已不存在：")
                + str(path)
                + "。这是普通文件操作，无需项目编码验收。",
            }
        return {
            "run_id": id,
            "status": run["status"],
            "gate": assess(self.store, run),
            "tasks": self.store.tasks(id),
            "files": changes(self.root, run)[0],
            "read_files": run.get("paths", []),
            "external_writes": run.get("external_writes", []),
            "error": run.get("error"),
        }

    def _finish(self, text: str) -> dict:
        if self.file_request and not self.runtime:
            if not self.file_outcome:
                text = "尚未确认删除：" + self.file_request + "。删除工具未成功执行。"
            elif self.file_outcome["exists_after"]:
                text = "未删除：" + self.file_outcome["path"] + "。检查发现目标文件仍然存在。"
        if self.runtime:
            gate = assess(self.store, self.runtime.run)
            self.runtime.run.update(
                status="completed" if gate["achieved"] else "incomplete", gate=gate
            )
        observed = (
            self.session.get("coding_turn") == self.session["turn"]
            or getattr(self, "observed_coding_turn", None) == self.session["turn"]
        )
        status = self._status() if observed and self.session.get("coding_run") else None
        file_status = status if status and status.get("kind") == "file_operation" else None
        if file_status:
            text, status = file_status["message"], None
        if status:
            if not status["gate"]["achieved"]:
                text = (
                    "编码尚未通过当前验收。\n"
                    + status["gate"]["reason"]
                    + "\n任务状态："
                    + status["status"]
                )
                if status.get("error"):
                    text += "\n" + status["error"]
                text += "\n可以说“继续刚刚的任务”，或查看任务状态后补充需求。"
            else:
                text += "\n[实际验收记录] 当前代码、长期规范与架构上的检查已通过，任务均已完成。"
            if status["external_writes"]:
                text += "\n目录外写入（尚未纳入项目验收）：\n" + "\n".join(
                    status["external_writes"]
                )
        if self.shell_result is not None and not status and not self.file_outcome:
            text += f"\n[命令结果] 退出码：{self.shell_result['exit_code']}"
        result = super()._finish(text)
        result["coding_result"] = status
        result["file_result"] = file_status or self.file_outcome
        return result

    def _code(self, request=None):
        if self.file_request:
            raise PermissionDenied("单文件删除不启动编码任务，请调用 delete_file")
        if self.session.get("coding_turn") == self.session["turn"]:
            return self._status()
        if self._pending() or self._pending_architecture():
            raise ValueError("请先确认或取消待处理的长期记忆草稿，再开始编码")
        if request is not None:
            run = new_run(self.store, self.repo, request, self.root, ["**"])
            run.update(chat_id=self.id, original_request=self.user_text)
            run["profile"] = {
                **run["profile"],
                "review": review_policy(self.store, self.model),
            }
            capture_baseline(self.store, run)
            self.session.update(coding_run=run["id"])
        else:
            id = self.session.get("coding_run")
            if not id:
                raise ValueError("本会话没有可继续的编码任务")
            run = self.store.get("run", id)
            if (
                run.get("chat_id") != self.id
                or run["repo_id"] != self.repo["id"]
                or Path(run["workspace"]).resolve() != self.root.resolve()
            ):
                raise ValueError("不能继续其他会话的任务")
            legacy_file = single_delete_request(run.get("original_request") or run["request"])
            if legacy_file:
                # 旧版本误建的删除任务恢复成普通操作，不重启测试循环。
                self.file_request = legacy_file
                result = self._execute("delete_file", {"path": str(Path(legacy_file).expanduser())})
                self.file_outcome["resumed"] = True
                return result
            if assess(self.store, run)["achieved"]:
                return self._status()
            run["profile"] = {
                **self.repo["profile"],
                "review": review_policy(self.store, self.model),
            }
        locks = self.store.directory / "locks"
        locks.mkdir(exist_ok=True)
        lock = (locks / f"{run['id']}.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError("同一运行已有进程在推进，不能同时续跑") from None
        self.run_lock = lock
        self.session["coding_turn"] = self.session["turn"]
        self._save()
        self.store.put("run", run["id"], run)
        print("\n[开始编码] " + str(self.root), flush=True)
        for record in self.store.list("verification"):
            if record["run_id"] == run["id"] and record["status"] == "running":
                record.update(
                    status="interrupted", passed=False, error="原验证进程已中断，请重新验证"
                )
                self.store.put("verification", record["id"], record)
        # Agent 仅复用工具和 Hooks 宿主，不启动它的模型循环。
        self.runtime = Agent(
            self.store,
            run,
            self.model,
            RuleSelector(self.store, "llm", self.model),
            self.skills,
            permissions=self.path_permissions,
        )
        self.runtime._event(
            "run_started",
            {
                "previous_status": run["status"],
                "previous_error": run.get("error"),
                "chat_id": self.id,
                "execution": "current_chat",
            },
        )
        if request is not None:
            self.runtime.hooks.trigger("UserPromptSubmit", run["request"])
        run.update(status="running", error=None)
        return self._status()

    def _execute(self, name: str, args: dict):
        if name not in self.schemas:
            raise ValueError("工具不在统一聊天白名单中")
        try:
            validate(args, self.schemas[name])
        except ValidationError:
            raise ValueError("工具参数不符合定义") from None
        if name == "glob":
            return self.path_permissions.glob(args["pattern"])
        if name == "compact":
            return super()._execute(name, args)
        if name == "begin_coding_task":
            return self._code(args["request"])
        if name == "resume_coding_task":
            return self._code()
        if name == "coding_status":
            self.observed_coding_turn = self.session["turn"]
            return self._status()
        if name == "set_test_runner":
            profile = dict(self.repo["profile"])
            python = (
                str(self.root / ".venv/bin/python")
                if (self.root / ".venv/bin/python").exists()
                else sys.executable
            )
            argv = {
                "pytest": [python, "-m", "pytest", "-q"],
                "unittest": [python, "-m", "unittest", "discover"],
                "compileall": [python, "-m", "compileall", "-q", "."],
            }[args["runner"]]
            profile.update(commands=[argv], runner=args["runner"])
            self.repo["profile"] = profile
            self.store.put("repo", self.repo["id"], self.repo)
            if self.session.get("coding_run"):
                run = self.store.get("run", self.session["coding_run"])
                if Path(run["workspace"]).resolve() == self.root.resolve():
                    run["profile"] = {
                        **profile,
                        "review": review_policy(self.store, self.model),
                    }
                    self.store.put("run", run["id"], run)
                    if self.runtime:
                        self.runtime.run["profile"] = run["profile"]
            return {"runner": args["runner"], "commands": [argv]}
        if name == "read_output":
            path = Path(args["path"]).resolve()
            if path.is_relative_to(self.compactor.directory.resolve()):
                return self.compactor.read_output(args)
            if not self.runtime:
                run = self._last_run()
                if not run:
                    raise ValueError("只能读取本会话归档的工具输出")
                return read_owned_output(self.store, run["id"], args)
        if self.runtime and name in {t["name"] for t in TOOLS}:
            block = {"name": name, "input": args}
            try:
                result = self.runtime.execute(name, args)
                self.runtime.hooks.trigger(
                    "PostToolUse", block, json.dumps(result, ensure_ascii=False)
                )
                return result
            finally:
                self.runtime._save(self.session["messages"])
        if name == "bash":
            if self.file_request:
                path = self.root / Path(self.file_request).expanduser()
                existed = path.exists() or path.is_symlink()
                result = self.shell.execute(args)
                if not result["timed_out"] and result["exit_code"] == 0:
                    self.file_outcome = {
                        "path": str(path),
                        "deleted": existed,
                        "exists_after": path.exists() or path.is_symlink(),
                    }
                    result["target_check"] = self.file_outcome
                return result
            self.shell_result = self.shell.execute(args)
            return self.shell_result
        if name in {"write_file", "edit_file"}:
            raw = args["path"]
            path = self.path_permissions.resolve(name, raw, "写入文件")
            if (self.root / raw).resolve() != path:
                raise PermissionDenied("写入目标在权限检查后发生变化，请重新请求")
            result = modify_text_file(path, name, args)
            print(f"[已写入文件] {path}", flush=True)
            return result
        if name == "list_tasks":
            run = self._last_run()
            return self.store.tasks(run["id"]) if run else []
        if name == "verification_status":
            run = self._last_run()
            if not run:
                raise ValueError("本会话没有可读取的编码验收记录")
            return verification_result(self.store, run["id"], args["id"])
        if name == "load_skill":
            skill = args["name"]
            if not skill or not all(c.isalnum() or c in "-_" for c in skill):
                raise ValueError("技能名称不合法")
            return (self.skills / skill / "SKILL.md").read_text(encoding="utf-8")
        if name == "delete_file":
            raw = args["path"]
            if (self.root / raw).is_symlink():
                raise PermissionDenied("delete_file 不删除符号链接，请用审批后的 Bash 明确操作")
            path = self.path_permissions.resolve(name, raw, "删除文件")
            result = delete_regular_file(self.root, raw, path)
            self.file_outcome = result
            print(
                ("[已删除文件] " if result["deleted"] else "[文件原本不存在] ") + str(path),
                flush=True,
            )
            return result
        if name == "read_file":
            path = self.path_permissions.resolve(name, args["path"], "读取文件")
            lines = path.read_text(encoding="utf-8").splitlines()
            offset, limit = max(0, args.get("offset", 0)), min(250, max(1, args.get("limit", 120)))
            return "\n".join(
                f"{i + 1}: {line}" for i, line in enumerate(lines) if offset <= i < offset + limit
            )
        if name in {t["name"] for t in TOOLS} - {"get_architecture"}:
            raise ValueError("请先 begin_coding_task 或 resume_coding_task，再使用编码和验收工具")
        return super()._execute(name, args)
