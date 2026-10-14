"""等待模型或宿主处理时给出终端进度，中断后立即停止提示线程。"""

import threading
import time

from repomentor.terminal_output import AssistantStream, print_final

LABELS = {
    "compact": "请求上下文压缩",
    "read_file": "读取文件",
    "glob": "查找文件",
    "list_files": "查看目录",
    "write_file": "写入文件",
    "edit_file": "修改文件",
    "delete_file": "删除文件",
    "begin_coding_task": "登记编码任务",
    "resume_coding_task": "恢复编码任务",
    "list_tasks": "查看任务",
    "create_task": "创建任务",
    "update_task": "更新任务",
    "verify": "启动后台验证",
    "wait_verification": "等待后台验证",
    "verification_status": "查询验证状态",
    "get_architecture": "读取架构",
    "list_rules": "读取长期规范",
    "load_skill": "加载技能",
    "read_output": "读取日志",
}


class Waiting:
    def __init__(self, label: str, interval=15, announce=True):
        self.label, self.interval, self.announce = label, interval, announce
        self.stop = threading.Event()
        self.quiet = threading.Event()
        self.worker = None

    def __enter__(self):
        self.started = time.monotonic()
        if self.announce:
            print(f"[{self.label}]", flush=True)
        self.worker = threading.Thread(target=self._report, daemon=True)
        self.worker.start()
        return self

    def _report(self):
        while not self.stop.wait(self.interval):
            if self.quiet.is_set():
                continue
            seconds = int(time.monotonic() - self.started)
            print(f"[{self.label}，已等待 {seconds} 秒；Ctrl+C 可中断]", flush=True)

    def __exit__(self, *_):
        self.stop.set()
        self.worker.join()


def request_reply(model, system, messages, tools):
    """正文实时显示，思考和工具参数仅显示阶段；工具仍须收齐参数才执行。"""
    output = AssistantStream()
    with Waiting("正在连接模型，等待首个响应") as progress:
        current, size = None, 0

        def update(event):
            nonlocal current, size
            if event.type == "content_block_start":
                block = event.content_block
                size = 0
                output.end_block()
                progress.quiet.clear()
                if block.type == "tool_use":
                    current = f"模型正在生成工具参数：{LABELS.get(block.name, block.name)}"
                elif block.type in {"thinking", "redacted_thinking"}:
                    current = "模型正在分析下一步"
                elif block.type == "text":
                    current = "模型正在生成回复"
                    output.begin_block()
                else:
                    return
                progress.label = current
                print(f"[{current}]", flush=True)
                if block.type == "text" and getattr(block, "text", ""):
                    progress.quiet.set()
                    output.write(block.text)
            elif event.type == "content_block_delta" and current:
                delta = event.delta
                text = getattr(delta, "text", "")
                if delta.type == "text_delta" and text:
                    progress.quiet.set()
                    output.write(text)
                # 工具参数只计数，内部思考不输出。
                size += len(getattr(delta, "partial_json", "") or text)
                progress.label = current + (f"，已接收 {size} 字符" if size else "")
            elif event.type == "content_block_stop":
                output.end_block()
                progress.quiet.clear()

        stream = getattr(model, "reply_stream", None)
        try:
            if callable(stream):
                reply = stream(system, messages, tools, on_event=update)
            else:
                reply = model.reply(system, messages, tools)
        finally:
            output.end_block()
    reply.displayed_text = output.text
    # 无流式能力的离线模型仍可展示工具调用前的说明。
    if not output.text and any(b.get("type") == "tool_use" for b in reply.content):
        for block in reply.content:
            if block.get("type") == "text" and block.get("text", "").strip():
                print_final(block["text"])
    return reply


def tool_started(name: str, args: dict):
    label = LABELS.get(name, "调用工具：" + name)
    detail = args.get("path") or args.get("pattern") or args.get("task_id") or args.get("id")
    if name == "update_task":
        detail = f"{detail} → {args.get('status', '')}"
    elif name == "get_architecture":
        detail = ", ".join(args.get("paths", []))
    if name != "bash":
        print(f"[{label}]" + (f" {detail}" if detail else ""), flush=True)


def tool_finished(name: str, result, failed=False):
    if failed:
        print(f"[工具失败] {name}：{str(result)[:300]}", flush=True)
    elif name in {"verify", "wait_verification", "verification_status"} and isinstance(
        result, dict
    ):
        id = result.get("verification_id") or result.get("id", "")
        state = result.get("status", "未知")
        if state == "running":
            status = result.get("stage", "仍在执行检查或独立评审")
        else:
            status = "通过" if result.get("passed") else "未通过"
        print(f"[验证状态] {id}：{status}（{state}）", flush=True)
    elif name != "bash":
        print(f"[工具完成] {LABELS.get(name, name)}", flush=True)
