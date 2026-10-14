"""普通文件操作的精确目标识别与删除后检查，不触发项目代码验收。"""

import re
from pathlib import Path

from repomentor.permissions import PermissionDenied


def modify_text_file(path: Path, name: str, args: dict) -> dict:
    """复用普通文本编辑动作；开发任务的架构约束由编码宿主在调用前检查。"""
    if name == "edit_file":
        text = path.read_text(encoding="utf-8")
        if not args["old_text"] or text.count(args["old_text"]) != 1:
            raise ValueError("旧文本必须非空且只匹配一次，请重新读取文件")
        content = text.replace(args["old_text"], args["new_text"], 1)
    else:
        content = args["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".repomentor-tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return {"written": str(path)}


def single_delete_request(text: str) -> str | None:
    # 这里只约束明确的单文件请求；复合开发需求仍交给模型规划。
    match = re.fullmatch(
        r"(?:请(?:你)?(?:帮我)?|帮我)?\s*(?:删除|删掉|移除|delete|remove)\s*"
        r"(?:(?:当前|本|这个)(?:工作)?目录(?:下(?:面|方)?|中|里(?:面)?)?(?:的)?\s*)?"
        r"(?:文件\s*)?(.+?)\s*[。！？!]?",
        text.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    name = match.group(1).strip()
    quote_pairs = {'"': '"', "'": "'", "`": "`", "“": "”", "‘": "’"}
    if len(name) > 1 and quote_pairs.get(name[0]) == name[-1]:
        name = name[1:-1]
    elif any(c.isspace() for c in name) or any(
        word in name for word in ("并", "然后", "同时", "以及", "中的", "里的", "和", "、")
    ):
        return None
    elif not (
        "/" in name
        or re.search(r"\.[A-Za-z0-9_-]+$", name)
        or re.fullmatch(r"[A-Za-z0-9_~-]+", name)
    ):
        return None
    if not name or any(c in name for c in "\0*?[];|&，,\n\r"):
        return None
    return name


def delete_regular_file(root: Path, raw_path: str, path: Path) -> dict:
    lexical = root / raw_path
    if lexical.is_symlink():
        raise PermissionDenied("delete_file 不删除符号链接；请用需要逐条审批的 Bash 明确操作链接")
    if lexical.resolve() != path:
        raise PermissionDenied("删除目标在权限检查后发生变化，请重新请求")
    if path.exists():
        if not path.is_file():
            raise ValueError("delete_file 只支持单个普通文件，不能删除目录或特殊文件")
        path.unlink()
        deleted = True
    else:
        deleted = False
    if path.exists() or path.is_symlink():
        raise ValueError("删除后目标仍然存在，请检查权限或并发写入")
    return {"path": str(path), "deleted": deleted, "exists_after": False}
