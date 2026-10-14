"""真实 stdio MCP 客户端；一个异步任务管理连接，同步编码循环通过队列调用。"""

import asyncio
import json
import os
import queue
import re
import threading
from concurrent.futures import Future
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

from jsonschema import validate
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    servers = config.get("servers")
    if not isinstance(servers, list) or not 1 <= len(servers) <= 4:
        raise ValueError("MCP 配置需包含 1 到 4 个可信 stdio 服务")
    names = set()
    for server in servers:
        name = server.get("name", "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,20}", name) or name in names:
            raise ValueError("MCP 服务名必须唯一且只包含字母、数字和下划线")
        names.add(name)
        if not isinstance(server.get("command"), str) or not server["command"]:
            raise ValueError("MCP command 必须是可信可执行程序")
        if server.get("cwd") is not None and not isinstance(server["cwd"], str):
            raise ValueError("MCP cwd 必须是可信工作目录字符串")
        if not isinstance(server.get("args", []), list) or not all(
            isinstance(a, str) for a in server.get("args", [])
        ):
            raise ValueError("MCP args 必须是参数数组")
        allowed = server.get("allowed_tools")
        if (
            not isinstance(allowed, list)
            or not allowed
            or len(allowed) > 20
            or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,30}", t) for t in allowed)
        ):
            raise ValueError("必须显式配置 MCP 工具白名单，不能使用通配符")
        if len(set(allowed)) != len(allowed):
            raise ValueError("MCP 白名单不能重复")
        if not isinstance(server.get("env_keys", []), list) or not all(
            isinstance(k, str) for k in server.get("env_keys", [])
        ):
            raise ValueError("MCP env_keys 只能列出需显式传递的环境变量名")
        if not 1 <= server.get("timeout", 30) <= 60:
            raise ValueError("MCP 超时必须介于 1 和 60 秒")
    return config


class MCPBridge:
    def __init__(self, config: dict):
        self.config, self.jobs, self.ready = config, queue.Queue(), Future()
        self.tools, self.routes = [], {}
        self.thread = threading.Thread(target=self._thread, daemon=True, name="repomentor-mcp")
        self.thread.start()
        try:
            self.ready.result(timeout=sum(s.get("timeout", 30) for s in config["servers"]) + 10)
        except Exception:
            self.close()
            raise ValueError("MCP 连接或工具校验失败，请检查可信服务和白名单") from None

    def _thread(self):
        try:
            asyncio.run(self._worker())
        except BaseException:
            if not self.ready.done():
                self.ready.set_exception(ValueError("MCP 启动失败"))
            while not self.jobs.empty():
                job = self.jobs.get_nowait()
                if job and not job[2].done():
                    job[2].set_exception(ValueError("MCP 连接已关闭"))

    async def _worker(self):
        async with AsyncExitStack() as stack:
            for config in self.config["servers"]:
                seconds = config.get("timeout", 30)
                keys = {
                    "PATH",
                    "HOME",
                    "TMPDIR",
                    "SystemRoot",
                    "USERPROFILE",
                    *config.get("env_keys", []),
                }
                env = {k: v for k, v in os.environ.items() if k in keys}
                streams = await stack.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=config["command"],
                            args=config.get("args", []),
                            env=env,
                            cwd=config.get("cwd"),
                        )
                    )
                )
                session = await stack.enter_async_context(
                    ClientSession(*streams, read_timeout_seconds=timedelta(seconds=seconds))
                )
                await session.initialize()
                found, cursor = {}, None
                for _ in range(10):
                    page = await session.list_tools(cursor=cursor)
                    for remote in page.tools:
                        if remote.name in found:
                            raise ValueError("MCP 服务返回重复工具名")
                        found[remote.name] = remote
                    cursor = page.nextCursor
                    if not cursor:
                        break
                else:
                    raise ValueError("MCP 工具目录超过读取预算")
                for allowed in config["allowed_tools"]:
                    remote = found.get(allowed)
                    if (
                        not remote
                        or not remote.annotations
                        or remote.annotations.readOnlyHint is not True
                    ):
                        raise ValueError("工具缺失或没有只读声明")
                    name = f"mcp__{config['name']}__{allowed}"
                    if name in self.routes:
                        raise ValueError("MCP 工具命名冲突")
                    if len(json.dumps(remote.inputSchema)) > 16000:
                        raise ValueError("MCP 工具参数结构超出预算")
                    self.routes[name] = (session, remote, seconds)
                    self.tools.append(
                        {
                            "name": name,
                            "description": (remote.description or "可信 MCP 只读工具")[:2000],
                            "input_schema": remote.inputSchema,
                        }
                    )
            self.ready.set_result(True)
            while True:
                job = await asyncio.to_thread(self.jobs.get)
                if job is None:
                    break
                name, arguments, future = job
                try:
                    session, remote, seconds = self.routes[name]
                    validate(arguments, remote.inputSchema)
                    result = await asyncio.wait_for(
                        session.call_tool(remote.name, arguments=arguments), seconds
                    )
                    if result.isError:
                        raise ValueError("MCP 服务报告工具执行失败")
                    if any(block.type != "text" for block in result.content):
                        raise ValueError("本阶段 MCP 查询仅支持文本和 JSON 返回值")
                    data = result.structuredContent
                    if data is None:
                        blocks = [
                            b.model_dump(exclude_none=True)
                            for b in result.content
                            if b.type == "text"
                        ]
                        if sum(len(b["text"]) for b in blocks) > 100000:
                            raise ValueError("MCP 文本返回值超出预算")
                        data = {"content": blocks}
                        if len(blocks) == 1:
                            try:
                                parsed = json.loads(blocks[0]["text"])
                                if isinstance(parsed, dict):
                                    data = parsed
                            except json.JSONDecodeError:
                                pass
                    if len(json.dumps(data, ensure_ascii=False)) > 100000:
                        raise ValueError("MCP 返回值超出预算，请缩小查询范围")
                    if not future.done():
                        future.set_result(data)
                except Exception as error:
                    if not future.done():
                        future.set_exception(ValueError(f"MCP 调用失败：{type(error).__name__}"))

    def call(self, name: str, arguments: dict):
        if name not in self.routes or not self.thread.is_alive():
            raise ValueError("MCP 工具不在已连接的白名单内")
        future = Future()
        self.jobs.put((name, arguments, future))
        return future.result(timeout=self.routes[name][2] + 5)

    def close(self):
        self.jobs.put(None)
        if self.thread.is_alive():
            self.thread.join(timeout=65)
        if self.thread.is_alive():
            raise ValueError("MCP 服务未能在超时内退出")
