# RepoMentor

**一个能记住项目规范和目录约定的 Coding Agent。**

把项目的开发规范、目录分工和日常反馈告诉 RepoMentor，确认后，后续开发就可以继续使用这些约定，不必每次重新交代。

## 适合什么场景

- **在已有项目里开发功能**：沿用现有代码风格、接口约定和目录分工。
- **反复提醒同一类要求**：例如日志脱敏、统一错误返回、分页参数限制。
- **团队有共同的开发规范**：把成员补充的约定整理下来，在后续任务中共同使用。
- **任务需要分几次完成**：中途退出后继续处理，查看还有哪些步骤没有完成。
- **写完代码需要检查**：要求 Agent 检查项目规范、文件位置并运行项目测试。

目前主要面向 Python 项目，使用方式是终端聊天。

## 开始使用

### 1. 安装

需要 Python 3.12 或以上，以及 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/jupiter12334/repo-mentor.git
cd repo-mentor
uv tool install --editable .
```

如果找不到 `repomentor` 命令，执行 `uv tool update-shell` 后重新打开终端。

### 2. 配置模型

创建 `~/.config/repomentor/.env`，填写模型配置。以下以 DeepSeek 为例，也可以参考 [.env.example](.env.example)：

```dotenv
ANTHROPIC_API_KEY=你的模型密钥
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
MODEL_ID=deepseek-flash
REVIEW_MODEL_ID=deepseek-flash
```

可以更换支持 Anthropic 消息接口的编码模型；当前配置的服务需要同时支持编码和评审使用的模型。

### 3. 进入项目，直接聊天

```bash
cd /你的项目目录
repomentor
```

启动时会显示当前工作目录。添加规范、查询约定、编写代码和查看进度，都在这个会话里完成。

## 先把项目约定告诉它

可以先添加最常用的规范，再随着开发逐步补充，不需要一次准备完整的开发文档。

```text
user: 添加长期规范：认证日志不能输出完整 token。
RepoMentor: 展示规范草稿，等待确认。
user: 确认。

user: 补充长期规范：列表接口的 limit 必须在 1 到 100 之间。
RepoMentor: 展示规范草稿，等待确认。
user: 确认。

user: 项目目录约定：backend/controllers 处理请求，backend/models 负责数据访问，frontend 放前端代码。
RepoMentor: 展示目录说明，等待确认。
user: 确认。
```

规范和目录说明需要确认后才会生效。单次任务里的临时要求，不会自动变成长期规范。

## 日常怎么用

直接说明要做的事情，以及需要保留的行为：

```text
user: 帮我给用户列表接口增加分页，保留现有返回格式，并运行测试。

user: 修复登录失败时返回信息不一致的问题，沿用项目现有的错误处理方式。

user: 当前项目有哪些长期规范？

user: backend/models 应该放哪些代码？

user: 看一下当前任务还有哪些步骤没完成。

user: 继续刚刚没有完成的任务。
```

开发完成后，RepoMentor 会检查修改是否符合项目约定，并报告检查和测试结果。项目有测试时默认使用 pytest，也可以在聊天里要求使用 unittest；没有测试时，默认只检查 Python 语法。测试依赖需要提前安装在项目环境中。

## 团队共同维护和共用

团队规范可以由成员共同维护，不需要只由一个人录入。开发过程中遇到新的约定、反复出现的问题，或者代码评审中达成的共识，都可以继续通过聊天补充给 Agent，确认后用于后续开发。

例如，一位成员补充“认证日志必须脱敏”，另一位成员补充“新增接口沿用统一错误返回格式”。使用同一项目记忆的成员，后续开发时就可以共同复用这些要求。

如果团队在同一开发环境中协作，可以使用大家都能访问的记忆目录启动：

```bash
cd /团队项目目录
repomentor --state-dir /团队共享目录/repomentor
```

**共用需要指向同一个项目和同一份记忆。** 不同电脑各自克隆项目时，目前不会自动同步规范；仅共享 Git 仓库还不够。

## 继续之前的会话

在聊天中输入 `/sessions` 查看会话，再用 `/resume 会话ID` 恢复。也可以在项目目录直接启动：

```bash
repomentor --session 会话ID
```

## 使用前了解

- 模型检查可能遗漏问题，仍需要项目测试和人工确认。
- 文件工具访问工作目录外的路径时会询问；Bash 通过命令限制后会直接执行，没有系统沙箱。
- 开源来源和复用说明见 [NOTICE.md](NOTICE.md)，许可证为 [MIT](LICENSE)。
