# RepoMentor

**受开发规范约束的 Coding Agent。**

写代码时，项目里通常已经有一套约定：接口放在哪里、哪些模块不能互相引用、日志里哪些内容要脱敏。这些要求可以写进开发文档，但换一个会话，或者任务做得太长，Agent 仍然可能遗漏。

RepoMentor 把这些规范和目录说明保存下来。接到开发任务后，它会读取相关约定，再修改代码、运行检查和评审，减少每次从头交代项目背景的麻烦。

技术栈：Python / Agent Harness / Context Engineering / Jev / MCP / SQLite

## 能做什么

### 记住项目约定

直接聊天添加开发规范或项目架构，确认后存入 SQLite，后续任务继续使用。不同项目的记忆分开保存，规范可以修改版本，也可以为具体文件设置例外。

目录说明用完整路径保存。例如 `backend/utils` 和 `frontend/utils` 各有自己的职责，不会因为都叫 utils 就混在一起。开发时只加载相关目录和父目录的说明。

### 把任务分步骤完成

开发任务可以拆成多个步骤，记录状态和前后依赖。检查和测试在后台执行，结果回来后再决定是否继续修改。任务和执行记录会保存，中断后可以继续；中断的后台检查需要重新运行。

### 长对话也能继续

工具输出过长时先保存原文，再缩短放入对话的内容。历史消息过多时分段归档，仍超出预算才生成摘要。压缩保留任务需求、工具调用与结果的对应关系，以及归档位置，需要时可以回读。

### 写完后检查代码

先运行静态检查和项目测试，再让 Jev 检查每个改动文件、相关规范、架构约束和任务目标。完整文件路径和源码一起提供，便于判断文件放置是否合适。

置信度足够的结果直接处理；置信度低、违规证据不足或 Jev 接口不可用时，交给独立的 `deepseek-flash` 复核。违规需要附上真实路径、行号和源码引用。代码或规范发生变化后，原来的验收结果不能继续使用。

## 安装

需要 Python 3.12 或以上，以及 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/jupiter12334/repo-mentor.git
cd repo-mentor
uv tool install --editable .
```

如果终端找不到 `repomentor`，执行 `uv tool update-shell` 后重新打开终端。

## 配置模型

在 `~/.config/repomentor/.env` 中填写配置，格式也可以参考 [.env.example](.env.example)：

```dotenv
ANTHROPIC_API_KEY=你的模型密钥
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
MODEL_ID=deepseek-flash
REVIEW_MODEL_ID=deepseek-flash
TYPESAFE_API_KEY=你的Jev密钥
```

编码模型可以更换，需支持 Anthropic 消息接口。Reviewer 默认使用 `deepseek-flash`，与编码模型分开配置；当前两者共用模型端点和密钥，因此端点需要支持相应模型。未配置 Jev 时会回退到 LLM 评审。

## 使用

进入要开发的项目目录，直接启动：

```bash
cd /你的项目目录
repomentor
```

启动后会显示当前工作目录。添加规范、查询记忆和编写代码都在同一个会话里完成：

```text
user: 添加长期规范：认证日志不能输出完整 token。
RepoMentor: 展示规范草稿，等待确认。
user: 确认。

user: 项目架构如下：backend/controllers 处理请求，backend/models 负责数据访问。
RepoMentor: 展示目录说明，等待确认。
user: 确认。

user: 当前项目有哪些长期规范？

user: 帮我给用户列表接口增加分页，保留现有返回格式，并运行测试。

user: 继续刚刚没有完成的任务。
```

临时开发需求不会自动成为长期规范；修改规范或架构草稿后，需要再次确认。

项目里有测试时，默认使用 pytest；没有测试时，默认只检查 Python 语法。也可以在聊天中要求使用 pytest 或 unittest。测试依赖需要安装在项目环境中，语法检查通过不代表业务功能正确。

记忆与运行记录默认保存在 `~/.local/share/repomentor`。通过 `repomentor --session 会话ID` 可以恢复会话，也可以在聊天里通过 `/sessions` 查看会话，再用 `/resume 会话ID` 恢复。

## 代码结构

```text
src/repomentor/
├── cli.py、interactive.py     命令行和聊天入口
├── agent.py、harness/         工具执行、Hooks 和上下文管理
├── store.py、architecture.py  项目记忆与目录说明
├── chat.py、learning.py       规范确认与反馈整理
├── verification.py           后台检查、测试和验收记录
├── judgement.py、review_all.py、reviewer.py  Jev 与 LLM 评审
└── mcp_client.py、mcp_server.py             MCP 接入
skills/                       可按需读取的开发流程
```

## 使用范围

目前主要面向 Python 项目，交互方式是终端聊天。模型评审仍可能误判，尤其是缺少相关依赖实现时，不能替代项目测试。

文件工具访问工作目录以外的路径时会询问。Bash 命中禁止命令会被拒绝，其余命令可直接执行；执行器没有系统沙箱，使用前请检查项目内容和模型配置。

支持通过 MCP 接入工具，也可以把项目规范和评审记录作为只读 MCP 工具提供。Harness 的开源来源和复用范围见 [NOTICE.md](NOTICE.md)，许可证为 [MIT](LICENSE)。
