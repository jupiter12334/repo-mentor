"""终端角色配色与增量输出；颜色只用于显示，不写入对话和长期记忆。"""

import os
import sys

RESET = "\033[0m"
USER_COLOR = "\033[1;96m"
ASSISTANT_COLOR = "\033[1;92m"


def color_enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.getenv("TERM") != "dumb"


def colored(text: str, color: str) -> str:
    return color + text + RESET if color_enabled() else text


class AssistantStream:
    def __init__(self):
        self.text = ""
        self.opened = False
        self.blocks = 0

    def begin_block(self):
        if self.blocks:
            self.text += "\n"
        self.blocks += 1

    def write(self, text: str):
        if not text:
            return
        if not self.opened:
            print(colored("RepoMentor:", ASSISTANT_COLOR), flush=True)
            self.opened = True
        sys.stdout.write(colored(text, ASSISTANT_COLOR))
        sys.stdout.flush()
        self.text += text

    def end_block(self):
        if self.opened:
            if not self.text.endswith("\n"):
                print(flush=True)
            self.opened = False


def print_final(message: str, displayed_text: str = ""):
    """流式正文已显示时只补充宿主验收结果，避免整段回复打印两次。"""
    if displayed_text and message.startswith(displayed_text):
        remainder = message[len(displayed_text) :].lstrip("\n")
        if remainder:
            print(colored(remainder, ASSISTANT_COLOR), flush=True)
        return
    stream = AssistantStream()
    stream.write(message)
    stream.end_block()
