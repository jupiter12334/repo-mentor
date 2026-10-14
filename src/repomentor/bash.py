"""借鉴 s15 的执行前 Hook；通过硬拒绝与参数检查后直接执行，不询问用户。"""

import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from repomentor.permissions import PermissionDenied
from repomentor.progress import Waiting

DENY_LIST = ("rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=")


def stop_process_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            # macOS 在组内只剩僵尸进程时可能返回 EPERM；回收已退出父进程。
            # 父进程仍存活则至少终止它，不能把清理异常掩盖成命令成功。
            if process.poll() is None:
                process.kill()
            break
        time.sleep(0.05)
    process.wait()


class BashRunner:
    def __init__(self, root: Path, archive: Path, event=None):
        self.root, self.archive = root.resolve(), archive
        self.event = event
        self.checked = None

    def _arguments(self, args: dict) -> tuple:
        command, timeout = args.get("command"), args.get("timeout", 120)
        if set(args) - {"command", "timeout"}:
            raise PermissionDenied("Bash 参数只能包含 command 和 timeout")
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise PermissionDenied("Bash 命令必须是非空字符串")
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise PermissionDenied("Bash 超时必须是 1 到 120 秒的整数")
        for pattern in DENY_LIST:
            if pattern in command:
                raise PermissionDenied(f"Bash 硬拒绝：命令含有 {pattern!r}")
        return command, timeout, str(self.root)

    def check(self, args: dict):
        self.checked = None
        key = self._arguments(args)
        if self.event:
            self.event(
                "bash_permission",
                {
                    "command": key[0],
                    "cwd": key[2],
                    "decision": "allow",
                    "automatic": True,
                    "reason": "通过 permission_hook 的硬拒绝与参数检查",
                },
            )
        # Hook 的检查绑定完整参数；后续 Hook 不能悄悄替换执行内容。
        self.checked = key

    def execute(self, args: dict) -> dict:
        key = self._arguments(args)
        if self.checked is None:
            self.check(args)
        checked, self.checked = self.checked, None
        if key != checked:
            raise PermissionDenied("Bash 参数在权限检查后发生变化，请重新请求执行")
        command, timeout, _ = key
        print(f"\n[Bash 执行] 工作目录：{self.root}；超时：{timeout} 秒\n{command}", flush=True)
        self.archive.mkdir(parents=True, exist_ok=True)
        log = self.archive / f"bash-{uuid.uuid4().hex[:12]}.log"
        # 不将宿主的模型密钥、GitHub 凭证等环境变量传给子进程。
        env = {
            k: v
            for k, v in os.environ.items()
            if not any(part in k.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        }
        venv = self.root / ".venv/bin"
        if venv.is_dir():
            env["PATH"] = str(venv) + os.pathsep + env.get("PATH", "")
        process, timed_out = None, False
        try:
            with log.open("wb") as output:
                process = subprocess.Popen(
                    ["/bin/bash", "-c", command],
                    cwd=self.root,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    with Waiting("Bash 正在运行", announce=False):
                        process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
        finally:
            # 正常退出时也清理同组残留子进程；Ctrl+C、超时走相同路径。
            if process is not None:
                stop_process_group(process)
        with log.open("rb") as output:
            preview = output.read(12000).decode("utf-8", errors="replace")
        result = {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "output": preview,
            "truncated": log.stat().st_size > 12000,
            "log": str(log),
        }
        if self.event:
            self.event("bash_finished", {"command": command, "cwd": str(self.root), **result})
        print(
            f"\n[Bash {'超时' if timed_out else '结束'}] 退出码：{process.returncode}", flush=True
        )
        if preview:
            print(preview[:2000].rstrip(), flush=True)
        if result["truncated"] or len(preview) > 2000:
            print(f"完整输出：{log}", flush=True)
        return result
