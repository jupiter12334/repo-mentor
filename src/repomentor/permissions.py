"""借鉴 s03 的执行前权限管线：硬拒绝、工作区判断、终端人工审批。"""

from pathlib import Path

from repomentor.terminal_input import read_line
from repomentor.workspace import protected_parts, safe_path


class PermissionDenied(ValueError):
    """权限拒绝需要完整取消同批剩余工具，再让模型重新规划。"""


def terminal_approval(tool: str, path: Path, operation: str) -> str:
    import sys

    if not sys.stdin.isatty():
        return "deny"
    print(f"\n[目录外访问确认]\n工具：{tool}\n操作：{operation}\n路径：{path}", flush=True)
    try:
        choice = read_line("允许吗？[y=仅本次 / a=本会话允许该路径的该操作 / N=拒绝]：").strip().lower()
    except EOFError:
        return "deny"
    return "session" if choice == "a" else "allow" if choice in {"y", "yes"} else "deny"


class PathPermissions:
    def __init__(self, root: Path, ask=terminal_approval, event=None, enabled: bool = True):
        self.root = root.resolve()
        self.ask, self.event, self.enabled = ask, event, enabled
        self.grants = set()

    def resolve(self, tool: str, name: str, operation: str) -> Path:
        if not isinstance(name, str) or not name or "\0" in name:
            raise PermissionDenied("文件路径无效")
        lexical = self.root / name
        path = lexical.resolve()
        relative = path.relative_to(self.root) if path.is_relative_to(self.root) else path
        lexical_relative = (
            lexical.relative_to(self.root) if lexical.is_relative_to(self.root) else lexical
        )
        if protected_parts(relative.parts) or protected_parts(lexical_relative.parts):
            raise PermissionDenied(
                "该路径受硬拒绝规则保护，不能读取或修改凭证、Git 元数据和运行状态"
            )
        if path.is_relative_to(self.root):
            return safe_path(self.root, str(path.relative_to(self.root)))
        if not self.enabled:
            raise PermissionDenied("路径越过工作目录边界")
        key = (str(path), operation)
        if key in self.grants:
            return path
        decision = self.ask(tool, path, operation)
        if self.event:
            self.event(
                "permission_decision",
                {"tool": tool, "path": str(path), "operation": operation, "decision": decision},
            )
        if decision not in {"allow", "session"}:
            raise PermissionDenied("开发者拒绝目录外访问；请改用工作目录内文件或重新规划")
        if decision == "session":
            self.grants.add(key)
        return path

    def glob(self, pattern: str) -> list[str]:
        if not pattern or "\0" in pattern:
            raise PermissionDenied("glob 模式无效")
        parts = Path(pattern).parts
        split = next((i for i, p in enumerate(parts) if any(c in p for c in "*?[")), len(parts))
        prefix = Path(*parts[:split]) if split else Path(".")
        base = self.resolve("glob", str(prefix), "列出目录")
        suffix = str(Path(*parts[split:])) if split < len(parts) else ""
        candidates = base.glob(suffix) if suffix else [base]
        matches = []
        for path in sorted(candidates):
            resolved = path.resolve()
            if not resolved.is_relative_to(base) or protected_parts(path.relative_to(base).parts):
                continue
            if path.is_file():
                matches.append(
                    path.relative_to(self.root).as_posix()
                    if path.is_relative_to(self.root)
                    else str(path)
                )
            if len(matches) >= 100:
                break
        return matches
