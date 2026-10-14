"""把一个已登记仓库的规范、运行状态和评审来源提供给 MCP 客户端。"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from repomentor.reviews import fetch_reviews
from repomentor.store import Store
from repomentor.verification import assess


def create_server(store: Store, repo: dict) -> FastMCP:
    server = FastMCP(
        "RepoMentor",
        instructions="只读查询当前绑定仓库；规范确认和写代码使用宿主 CLI。",
        log_level="WARNING",
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=readonly)
    def list_rules(include_candidates: bool = False) -> dict:
        """读取当前仓库的规范，候选不会被当作生效规范。"""
        return {
            "repo_id": repo["id"],
            "rules": [
                r.to_dict() for r in store.rules(repo["id"], active_only=not include_candidates)
            ],
        }

    @server.tool(annotations=readonly)
    def get_run_status(run_id: str) -> dict:
        """查看本仓库的任务与当前版本验收结果，不返回原始对话或认证信息。"""
        run = store.get("run", run_id)
        if run["repo_id"] != repo["id"]:
            raise ValueError("运行不属于当前 MCP 仓库")
        return {
            "run_id": run_id,
            "status": run["status"],
            "gate": assess(store, run),
            "tasks": store.tasks(run_id),
        }

    @server.tool(annotations=readonly)
    def get_review_batch(batch_id: str) -> dict:
        """读取本仓库已经缓存的 PR/MR 评审及可导入状态。"""
        batch = store.get("review_batch", batch_id)
        if batch["repo_id"] != repo["id"]:
            raise ValueError("评审批次不属于当前 MCP 仓库")
        return batch

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    )
    def preview_reviews(url: str) -> dict:
        """从绑定仓库的 PR/MR 只读获取评审；本地缓存不改变代码或生效规范。"""
        return fetch_reviews(store, repo, url)

    return server
