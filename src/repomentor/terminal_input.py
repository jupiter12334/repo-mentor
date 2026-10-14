"""复用课程 s01/s03 的 UTF-8 行编辑配置，避免中文退格产生残影和字节残留。"""

import sys
from functools import cache

from repomentor.terminal_output import RESET, USER_COLOR, color_enabled


@cache
def configure_readline() -> bool:
    # 延迟初始化，避免只读 MCP 或不需要输入的命令触碰终端行编辑状态。
    try:
        import readline
    except ImportError:
        return False
    # 与 learn-claude-code 的配置一致，兼容 macOS libedit 和 GNU readline。
    for setting in (
        "set bind-tty-special-chars off",
        "set input-meta on",
        "set output-meta on",
        "set convert-meta off",
    ):
        readline.parse_and_bind(setting)
    return True


def read_line(prompt: str) -> str:
    configure_readline()
    return input(prompt)


def read_user_line() -> str:
    """提示符和正在编辑的输入使用同一颜色，非打印标记避免中文退格残影。"""
    configured = configure_readline()
    if not color_enabled() or not sys.stdin.isatty():
        return input("user: ")
    # 与课程 s15 相同，用 \001/\002 告诉行编辑器 ANSI 转义没有显示宽度。
    prefix = "\001" + USER_COLOR + "\002" if configured else USER_COLOR
    try:
        return input(prefix + "user: ")
    finally:
        # 正常返回、EOF 和 Ctrl+C 均复位，避免进度及后续权限询问沿用输入颜色。
        print(RESET, end="", flush=True)
