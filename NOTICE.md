# 来源与许可证

项目的 Agent Harness 参考并复用了 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的部分实现，固定版本为 `0dcafa2ae053a1ddd6a72f265431104b08a5aa13`。上游采用 MIT 许可证，版权为 Copyright (c) 2024 shareAI Lab，完整许可证保留在根目录 `LICENSE`。

直接复用的部分：

- `harness/course_core.py`：s15 中的工具消息边界函数。
- `harness/s08_compact.py`：s08 的 `ContextCompactor` 类，保留原算法；`harness/context.py` 负责项目适配，并把历史消息裁剪改为超过 70 条触发、目标 50 条，只归档中间部分。
- `terminal_input.py`：参考 s01/s03 的四条 readline 配置。
- `bash.py`：沿用 s15 的六个命令拒绝字符串，执行器、参数检查和日志归档为本项目实现。

工具循环、Hooks、任务依赖和后台检查参考 s15；结构化步骤日志参考 s16；MCP 工具发现与分发参考 s14，并使用官方 MCP Python SDK。项目规范和架构的 SQLite 存储、人工确认、版本管理、任务快照验收、反馈学习、统一聊天入口及 Jev / LLM 评审分流为本项目实现。
