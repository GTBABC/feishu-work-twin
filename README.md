# Feishu Work Twin

本地飞书工作数字人：使用个人账号接收消息，通过 ChatGPT 账号登录的 Codex CLI 处理会话，并提供本机网页工作台。

基于 [cv-cat/LarkAgentX](https://github.com/cv-cat/LarkAgentX) 二次开发。保留原项目历史与协议实现，原始说明见 [README.upstream.md](README.upstream.md)。

## 功能

- 私聊根据语境自动回复；群聊默认不自动回复，可逐群选择闲聊模式、仅 @我、仅 @全体或关闭。
- 连续短句按最后一句后的停顿合并；群里单独 @本人时带上同一话题的前文和近期图片。
- 每个联系人、群聊可独立设置自动回复、仅草稿或关闭。
- 人工发送后，该会话暂停自动回复 2 分钟；暂停期间的新消息按最后一次活动延长计时，旧消息不会到期补发。
- 会话概览、有效记忆、联系人角色与回复风格、异常与待办管理。
- 网页支持图片、表情包、图文消息显示，以及图片选择、粘贴、拖入、混排和发送前预览。
- AI 回复润色、文字尾缀、执行记录、左下角数字人入口及居中聊天窗口。
- 数字人控制台可查询工作、创建或完成待办，以及按明确指令发送消息；已建立的会话才能作为收件人。

## 安装与启动

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/) 和已安装的 Codex CLI。

```powershell
git clone https://github.com/jadenjin/feishu-work-twin.git
cd feishu-work-twin
Copy-Item .env.example .env
uv sync --locked
codex login
uv run lark auth qr
uv run lark web --port 8765
```

打开 http://127.0.0.1:8765/。Windows 也可运行 `start-workhub.ps1`；已运行时不会启动第二个监听实例。

`.env.example` 默认使用 Codex 后端、独立会话上下文、群聊默认不回复、不自动标记已读。若 Codex 不在 PATH，填写 `.env` 中的 `LARKX_CODEX_PATH`。模型可通过 `LARKX_CODEX_MODEL` 配置，留空使用 CLI 默认模型。

网页只绑定 `127.0.0.1`。凭证、消息数据库、图片缓存与模型会话保存在 `LARKX_HOME`，默认 `~/.larkx`。这些数据不纳入 Git。不要同时启动 `lark listen --agent` 与网页监听，否则可能重复处理消息。

在“数字人设置”可调整消息停顿时间（0.1–30 秒）和最长等待时间（0.1–60 秒），保存后下一批消息生效，重启后保留。未在网页设置时，`LARKX_BATCH_DELAY` 默认 1.5 秒，收到新句子后重新计时；`LARKX_BATCH_MAX_WAIT` 默认 5 秒，避免连续来消息时一直等待。一个批次最多 20 条。群聊按各群的回复选项触发；选择仅 @我或仅 @全体时，短暂等待期间的未 @补充句会一起理解，其他群消息不会独立触发回复。

## 操作

进入“会话与记忆”选择联系人，编辑消息后点击发送。Enter 换行，Ctrl + Enter 发送。图片最多 6 张，单张不超过 10 MB，支持 PNG、JPEG、GIF、WebP。

“角色档案”设置关系、背景、口吻与独立回复开关。“数字人设置”配置本人工作原则和文字尾缀；尾缀留空可关闭。AI 润色生成的草稿需要手动发送。

控制台的明确发送指令会执行。发送结果无法确认时保留为待核对，不自动重发；提交相同请求 ID 不会再次发送。实际编码、排期、审批等工作不会仅凭聊天请求被标记为已完成。

## MCP 接入

启动工作台后，本机 MCP 地址为 `http://127.0.0.1:8765/mcp`，传输方式选 **Streamable HTTP**。MCP 与网页共用同一个飞书连接，不需要重新登录。当前只供本机应用访问，不支持云端应用直接连接。

仅支持 stdio 的应用，可参考 [mcp-config.example.json](mcp-config.example.json) 配置命令，修改为自己的 uv 和项目路径。实际启动命令为：

```powershell
uv run --directory E:\Myproject\LarkAgentX --no-sync python -m larkx.workhub.mcp_server
```

工作台使用其他端口时，在命令最后添加 `--hub-url http://127.0.0.1:其他端口`。

提供 `get_status`、`search_conversations`、`get_chat_history`、`search_messages`、`send_message` 五个工具。记录来自本机已同步的数据库，并非飞书全部历史；查询返回 `next_before_id` 可翻页，会话搜索使用 `next_offset` 翻页。

先搜索联系人或群聊，取得明确的 `chat_id`，再发送。重名会返回多个会话，不自动猜测。发送需指定消息正文和唯一 `request_id`（建议 UUID），同一发送的重试必须复用 ID。仅 `status=sent` 表示成功，`uncertain` 应先在飞书核对，不换 ID 重发。发送沿用设置中的文字尾缀，写入执行记录，并触发 2 分钟人工接管。

客户端提示示例：“查边泽文最近 30 条聊天记录”“搜索关于上线排期的消息”“给边泽文发送：下午三点开会”。请在客户端启用发送工具的确认提示。

MCP 协议实现使用 [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)。

## 开发验证

```powershell
uv sync --locked --group dev
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync ruff check larkx/workhub larkx/agent/codex_backend.py larkx/media.py tests --select F
node --check larkx/workhub/static/app.js
uv build
```

测试使用模拟发送接口和临时数据库，不登录飞书，不发送真实消息，也不需要安装 Codex CLI。GitHub Actions 在 Windows 与 Linux 上运行同一套测试。

## 实现与限制

`larkx/workhub` 包含 FastAPI 本地服务、SQLAlchemy 状态存储和前端静态文件；`CodexBackend` 使用独立临时目录与只读沙箱生成结构化分析。

个人账号消息通道依赖飞书网页私有协议，后续飞书更新可能影响登录、消息或资源上传；该项目并非飞书官方客户端。聊天历史与必要图片会作为模型上下文提交到已登录的模型服务。仅在本机环境使用，勿把控制台直接暴露到公网。

群聊“闲聊模式”会让 AI 结合上下文判断是否参与：优先回应对我说的话，别人之间的讨论通常不接话，有趣或能补充信息时可偶尔插一句，并非每条都回复。闲聊模式下真实 @我（包括单独一个 @）必须回应，仍遵守人工接管、全局暂停和独立关闭开关。
