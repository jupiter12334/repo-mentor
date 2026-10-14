"""中文命令行：仓库登记、反馈学习、规范确认、编码和续跑。"""

import argparse
import json
import os
import signal
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from repomentor.agent import Agent, new_run
from repomentor.architecture import ArchitectureMemory, architecture_context
from repomentor.learning import continue_learning, new_learning
from repomentor.mcp_client import load_config
from repomentor.providers.anthropic import AnthropicModel
from repomentor.reviewer import review_policy
from repomentor.reviews import fetch_reviews, learn_review
from repomentor.selection import RuleSelector, digest
from repomentor.store import Store
from repomentor.terminal_input import read_user_line
from repomentor.terminal_output import print_final
from repomentor.verification import assess
from repomentor.workspace import create_worktree, repo_identity

PROJECT = Path(__file__).resolve().parents[2]
SKILLS = (
    PROJECT / "skills"
    if (PROJECT / "skills").exists()
    else Path(__file__).parent / "resources/skills"
)


def output(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def registered(store: Store, path: str) -> dict:
    return store.get("repo", repo_identity(Path(path)))


def default_state_directory(root: Path) -> Path:
    old = root / ".repomentor"
    if (old / "state.sqlite3").exists():
        return old
    # 源码安装中旧 CLI 以工具工程为起点保存状态，按项目身份识别后继续使用。
    legacy = PROJECT / ".repomentor"
    if (legacy / "state.sqlite3").exists():
        try:
            Store(legacy).get("repo", repo_identity(root))
            return legacy
        except ValueError:
            pass
    return Path.home() / ".local/share/repomentor"


def register_repo(store: Store, path: Path, profile_path: Path, python: str | None = None) -> dict:
    path = path.resolve()
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    # 保留虚拟环境解释器的符号链接路径，resolve 会错误地退回无依赖的基础 Python。
    interpreter = Path(python).absolute() if python else path / ".venv/bin/python"
    if not interpreter.exists():
        raise ValueError("验证对象缺少虚拟环境，请先安装其依赖，或用 --python 指定解释器")
    commands = profile["commands"]
    if not commands or not all(isinstance(command, list) and command for command in commands):
        raise ValueError("至少需要配置一个非空检查命令")
    profile["commands"] = [
        [arg.replace("{python}", str(interpreter)) for arg in command] for command in commands
    ]
    record = {"id": repo_identity(path), "path": str(path), "profile": profile}
    store.put("repo", record["id"], record)
    return record


def start_run(
    store: Store,
    repo: dict,
    request: str,
    allowed: list[str],
    paths: list[str] | None = None,
    in_place: bool = False,
    mode: str = "live",
) -> dict:
    run = new_run(store, repo, request, Path(repo["path"]), allowed, paths, mode)
    if not in_place:
        run["workspace"] = str(create_worktree(Path(repo["path"]), store.directory, run["id"]))
        store.put("run", run["id"], run)
    return run


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="RepoMentor：团队规范学习型编码智能体")
    p.add_argument("--state-dir", help="可选的状态目录；通常无需指定")
    p.add_argument("--env-file", help="可选的模型配置文件；通常使用全局配置")
    p.add_argument("--session", help="恢复聊天会话；也可在聊天里使用 /resume")
    p.add_argument("--message", help="发送单条消息；省略时进入聊天")
    p.set_defaults(command="chat", repo=None, max_rounds=30)
    commands = p.add_subparsers(dest="command", required=False)
    architecture = commands.add_parser("architecture", help="提出、确认或查询按目录存储的项目架构")
    architecture.add_argument("action", choices=["show", "propose", "confirm"])
    architecture.add_argument("--file", help="完整架构 YAML 或 JSON；提出后仍需确认")
    architecture.add_argument("--version", type=int, help="候选版本或要查看的历史版本")
    architecture.add_argument("--path", action="append", help="查看指定完整路径及父目录职责")
    init = commands.add_parser("init", help="登记已有 Git 仓库和验证配置")
    init.add_argument("--profile", required=True)
    init.add_argument("--python")
    rules = commands.add_parser("rules", help="查看、提出或确认仓库规范")
    rules.add_argument("action", choices=["list", "propose", "confirm"])
    rules.add_argument("--id")
    rules.add_argument("--version", type=int)
    rules.add_argument("--requirement")
    rules.add_argument("--scope", action="append")
    rules.add_argument("--condition", default="")
    rules.add_argument("--exceptions", default="")
    rules.add_argument("--source", default="人工评审反馈")
    rules.add_argument("--checker", help="检查器 JSON 文件")
    rules.add_argument("--mandatory", action="store_true")
    rules.add_argument("--all", action="store_true")
    rules.add_argument("--parent", help="具体文件例外所绑定的父规范 ID")
    rules.add_argument("--parent-version", type=int)
    rules.add_argument("--replace", help="确认时明确替代的旧规范 ID")
    rules.add_argument("--replace-version", type=int)
    feedback = commands.add_parser("feedback", help="可恢复的反馈提炼与规范关联工作流")
    feedback.add_argument("--text")
    feedback.add_argument("--scope", action="append")
    feedback.add_argument("--resume", help="继续已有反馈工作流 ID")
    feedback.add_argument("--id", help="修订旧规范时填写它的 ID")
    feedback.add_argument("--checker")
    feedback.add_argument("--mandatory", action="store_true")
    feedback.add_argument("--parent", help="提出具体文件例外时填写父规范 ID")
    feedback.add_argument("--parent-version", type=int)
    chat = commands.add_parser("chat", help="统一聊天：规范、架构、编码和任务查询")
    chat.add_argument("--session", default=argparse.SUPPRESS, help="继续已有聊天会话")
    chat.add_argument("--message", default=argparse.SUPPRESS, help="发送一条开发者消息")
    chat.add_argument("--max-rounds", type=int, default=12)
    run = commands.add_parser("run", help="执行编码任务，默认建立独立工作副本")
    run.add_argument("--request", required=True)
    run.add_argument("--allow", action="append", required=True, help="允许修改的相对路径或通配符")
    run.add_argument("--path", action="append", help="已知相关文件，用于规范范围过滤")
    run.add_argument("--in-place", action="store_true", help="直接在登记仓库内修改")
    run.add_argument("--no-review", action="store_true", help="显式关闭语义评审，用于消融评测")
    run.add_argument("--mcp-config", help="用户审核过的 stdio MCP 服务和工具白名单 JSON")
    resume = commands.add_parser("resume", help="继续未完成的编码任务")
    resume.add_argument("--run", required=True)
    for command in (run, resume):
        command.add_argument("--selector", choices=["llm"], default="llm")
        command.add_argument("--max-rounds", type=int, default=20)
    status = commands.add_parser("status", help="查看任务、验收与模型使用记录")
    status.add_argument("--run", required=True)
    reviews = commands.add_parser("reviews", help="读取 PR/MR 文件评审并提炼候选规范")
    reviews.add_argument("action", choices=["fetch", "show", "learn"])
    reviews.add_argument("--url")
    reviews.add_argument("--batch")
    reviews.add_argument("--comment")
    reviews.add_argument("--scope", action="append")
    reviews.add_argument("--checker")
    reviews.add_argument(
        "--historical", action="store_true", help="显式学习已解决或过期评论；需指定 --scope"
    )
    commands.add_parser("mcp", help="通过 stdio 提供当前仓库的只读 MCP 工具")
    p.set_defaults(command="chat")
    return p


def summary(store: Store, run: dict) -> dict:
    events = store.events(run["id"])
    return {
        "run_id": run["id"],
        "mode": run.get("mode", "live"),
        "status": run["status"],
        "workspace": run["workspace"],
        "error": run.get("error"),
        "gate": assess(store, run),
        "tasks": store.tasks(run["id"]),
        "model_calls": [e["data"] for e in events if e["type"] == "model_reply"],
        "rule_selection": [e["data"] for e in events if e["type"] == "rule_selection"],
        "verifications": [v for v in store.list("verification") if v["run_id"] == run["id"]],
    }


def dispatch(args, store: Store) -> int:
    args.repo = str(Path.cwd().resolve())
    # 工作目录只取启动位置，不允许模型或管理命令隐式切换项目。
    if args.command == "chat":
        from repomentor.interactive import current_project

        current_project(store, Path(args.repo))
    if args.command == "init":
        output(register_repo(store, Path(args.repo), Path(args.profile), args.python))
    elif args.command == "architecture":
        repo = registered(store, args.repo)
        memory = ArchitectureMemory(store)
        if args.action == "propose":
            if not args.file:
                raise ValueError("提出架构需要 --file；文件只作为候选，确认后才生效")
            try:
                spec = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
            except yaml.YAMLError:
                raise ValueError("架构文件不是合法的 YAML 或 JSON") from None
            output(memory.propose(repo["id"], spec, "人工导入：" + str(Path(args.file).resolve())))
        elif args.action == "confirm":
            if not args.version:
                raise ValueError("确认架构需要明确指定 --version")
            output(memory.confirm(repo["id"], args.version))
        else:
            record = memory.get(repo["id"], args.version)
            output(architecture_context(record, args.path) if args.path else record)
    elif args.command == "rules":
        repo = registered(store, args.repo)
        if args.action == "list":
            output([r.to_dict() for r in store.rules(repo["id"], active_only=not args.all)])
        elif args.action == "confirm":
            if not args.id:
                raise ValueError("确认规范需要 --id")
            candidates = [r for r in store.rules(repo["id"], active_only=False) if r.id == args.id]
            latest = max(candidates, key=lambda r: r.version) if candidates else None
            if latest and latest.status == "candidate" and latest.provenance:
                if args.version != latest.version:
                    raise ValueError("确认评审候选需要明确指定当前 --version")
            output(
                store.confirm(
                    repo["id"],
                    args.id,
                    args.version,
                    replace=args.replace,
                    replace_version=args.replace_version,
                ).to_dict()
            )
        else:
            if not args.requirement:
                raise ValueError("提出规范需要 --requirement")
            checker = json.loads(Path(args.checker).read_text()) if args.checker else None
            rule = store.propose(
                repo["id"],
                args.requirement,
                id=args.id,
                paths=args.scope,
                condition=args.condition,
                exceptions=args.exceptions,
                source=args.source,
                mandatory=args.mandatory,
                checker=checker,
                kind="exception" if args.parent else "requirement",
                parent_id=args.parent,
                parent_version=args.parent_version,
            )
            output(rule.to_dict())
    elif args.command == "feedback":
        if args.resume:
            id = args.resume
        else:
            if not args.repo or not args.text or not args.scope:
                raise ValueError("新反馈需要 --text 和 --scope；续跑只需 --resume")
            repo = registered(store, args.repo)
            checker = json.loads(Path(args.checker).read_text()) if args.checker else None
            record = new_learning(
                store,
                repo["id"],
                args.text,
                args.scope,
                id=args.id,
                mandatory=args.mandatory,
                checker=checker,
                kind="exception" if args.parent else "requirement",
                parent_id=args.parent,
                parent_version=args.parent_version,
            )
            id = record["id"]
        print(f"反馈工作流：{id}", flush=True)
        record = continue_learning(store, id, AnthropicModel())
        output(record)
    elif args.command == "chat":
        from repomentor.chat import new_chat
        from repomentor.interactive import UnifiedChat

        repo = registered(store, args.repo)
        session_id = args.session or new_chat(store, repo)["id"]
        print(f"当前工作目录：{args.repo}", flush=True)
        print("默认直接在此目录修改代码；访问目录外文件时会询问权限。", flush=True)
        print("Bash 通过权限 Hook 后直接执行，无需确认；执行器没有系统沙箱。", flush=True)
        agent = UnifiedChat(store, repo, session_id, AnthropicModel(), SKILLS, args.max_rounds)
        print(f"聊天会话：{session_id}", flush=True)
        if args.message is not None:
            result = agent.send(args.message)
            print_final(result["message"], agent.displayed_text)
            if result.get("coding_result") and not result["coding_result"]["gate"]["achieved"]:
                return 2
        else:
            print(
                "直接描述需求。可添加规范、登记架构、查询记忆或编写代码。/help 查看帮助，/exit 退出。"
            )
            while True:
                try:
                    text = read_user_line().strip()
                except EOFError:
                    break
                if text in {"退出", "/exit", "/quit"}:
                    break
                if text == "/help":
                    print(
                        "直接聊天即可。/new 新会话，/sessions 查看会话，/resume 会话ID 恢复，/exit 退出。"
                    )
                    continue
                if text == "/sessions":
                    sessions = [s for s in store.list("chat") if s["repo_id"] == repo["id"]]
                    for s in sessions[-10:]:
                        print(f"{s['id']}（{s['turn']} 轮）")
                    continue
                if text == "/new" or text.startswith("/resume "):
                    session_id = (
                        new_chat(store, repo)["id"]
                        if text == "/new"
                        else text.split(maxsplit=1)[1].strip()
                    )
                    try:
                        agent = UnifiedChat(
                            store, repo, session_id, agent.model, SKILLS, args.max_rounds
                        )
                    except ValueError as error:
                        print(f"无法恢复会话：{error}")
                        continue
                    print("当前聊天会话：" + session_id)
                    continue
                if text:
                    try:
                        result = agent.send(text)
                        print_final(result["message"], agent.displayed_text)
                    except KeyboardInterrupt:
                        print(
                            "\n已中断当前任务，留在本会话。可以说‘继续刚刚的任务’或输入 /exit。",
                            flush=True,
                        )
    elif args.command in {"run", "resume"}:
        model = AnthropicModel()
        backend = args.selector
        selector_model = (
            AnthropicModel(os.getenv("SELECTOR_MODEL_ID") or None) if backend == "llm" else None
        )
        selector = RuleSelector(store, backend, selector_model)
        if args.command == "run":
            repo = registered(store, args.repo)
            mcp_config = load_config(Path(args.mcp_config)) if args.mcp_config else None
            run = start_run(store, repo, args.request, args.allow, args.path, args.in_place)
            run["profile"] = {
                **run["profile"],
                "review": review_policy(store, model, not args.no_review),
            }
            if mcp_config:
                run["mcp_config"] = mcp_config
                run["profile"]["mcp_config_hash"] = digest(run["mcp_config"])
            store.put("run", run["id"], run)
        else:
            run = store.get("run", args.run)
            policy = run["profile"].get("review", {})
            current_policy = review_policy(store, model, policy.get("enabled", False))
            if policy.get("enabled") and policy != current_policy:
                run["profile"] = {
                    **run["profile"],
                    "review": current_policy,
                }
                store.put("run", run["id"], run)
                print("Reviewer 策略已更新，旧验收证据失效，需要重新验证。", flush=True)
            if run["status"] == "completed" and assess(store, run)["achieved"]:
                output(summary(store, run))
                return 0
        print(f"运行：{run['id']}\n工作副本：{run['workspace']}", flush=True)
        run = Agent(store, run, model, selector, SKILLS, args.max_rounds).run_loop()
        output(summary(store, run))
        return 0 if run["status"] == "completed" and assess(store, run)["achieved"] else 2
    elif args.command == "status":
        output(summary(store, store.get("run", args.run)))
    elif args.command == "reviews":
        repo = registered(store, args.repo)
        if args.action == "fetch":
            if not args.url:
                raise ValueError("获取评审需要 --url")
            output(fetch_reviews(store, repo, args.url))
        else:
            if not args.batch:
                raise ValueError("读取或学习评审需要 --batch")
            batch = store.get("review_batch", args.batch)
            if batch["repo_id"] != repo["id"]:
                raise ValueError("评审批次不属于当前仓库")
            if args.action == "show":
                output(batch)
            else:
                if not args.comment:
                    raise ValueError("提炼评审需要明确指定 --comment")
                checker = json.loads(Path(args.checker).read_text()) if args.checker else None
                output(
                    learn_review(
                        store,
                        repo,
                        args.batch,
                        args.comment,
                        AnthropicModel(),
                        paths=args.scope,
                        checker=checker,
                        historical=args.historical,
                    )
                )
    elif args.command == "mcp":
        from repomentor.mcp_server import create_server

        create_server(store, registered(store, args.repo)).run(transport="stdio")
    return 0


def main():
    args = parser().parse_args()
    # 只读 MCP 服务无需主模型凭证；专用 API 凭证通过客户端 env_keys 显式传入。
    if args.command != "mcp":
        if args.env_file:
            load_dotenv(args.env_file, override=False)
        else:
            # 全局配置优先；源码安装可继续读取现有接口配置，不复制密钥。
            load_dotenv(Path.home() / ".config/repomentor/.env", override=False)
            load_dotenv(PROJECT / ".env", override=False)
    # 正常终止与 Ctrl-C 共用清理路径，后台进程由运行时收尾。
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        state = (
            Path(args.state_dir)
            if args.state_dir
            else default_state_directory(Path.cwd().resolve())
        )
        status = dispatch(args, Store(state))
    except KeyboardInterrupt:
        print(
            "对话已中断，状态已保存；在原目录启动 repomentor --session 会话ID 继续。"
            if args.command == "chat"
            else "运行已中断，状态已保存；可以使用 resume 继续。",
            file=sys.stderr,
        )
        status = 130
    except (ValueError, OSError, KeyError) as error:
        print(f"错误：{error}", file=sys.stderr)
        status = 2
    except Exception as error:
        # 接口错误可能带响应正文，不把原始正文或认证信息打印到终端。
        print(f"执行失败：{type(error).__name__}。请检查接口配置及本地状态。", file=sys.stderr)
        status = 2
    raise SystemExit(status)
