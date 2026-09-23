# Changelog

版本号遵循 semver；宿主按 tag 更新（见 README「宿主如何更新」）。

## v0.2.0 — 2026-09-24

- **发图片**：`POST /files`（请求体即图片，按魔数认 PNG / JPEG / GIF / WebP）、发送时 `files: [id]` 绑定到用户消息；worker 遇到带图的消息改用 `claude -p --input-format stream-json` 把图片直接放进用户消息。浏览器端 `planImage / prepareImages / mountAttachments`：只做尺寸上限不压画质（长边 > 2000px 等比缩，长截图切段）
- **模型列表按 worker 本机 CLI 动态上报**：零费用的 `claude -p "/model" --no-session-persistence` 探测别名（跟随 CLI 最新）与宿主候选的固定版本；启动时、每 6 小时、CLI 版本变化时重探；`GET /settings` 返回分组列表，前端 `modelOptionsHtml`
- **压缩会话看得到进度**：session 任务排队 / 开始 / 结束 / 心跳超时都推 `job` 帧，快照带进行中的任务，完成后在对话里留一行；`/compact` 运行中每 15 秒心跳（修复超过 120 秒被判超时）
- **界面照 Claude Code 客户端重做**：通栏轮次、工具调用折叠成一行并与正文按真实顺序交错（`splitTurn`）、每台设备自己的界面偏好（`renderPrefsPanel`）、会话信息收进底部弹层、手机上的抽屉 / 遮罩 / 背景锁定
- **版本**：`claude_bridge.__version__` / `REPO`；`GET /settings`、`GET /status` 带 `bridge: {version, repo, helper_version}`；浏览器 `checkBridgeUpdate` 查 GitHub 最新 tag

## v0.1.0 — 2026-09-22

- 首版：浏览器 ⇄ 服务端（FastAPI + SQLite 任务队列）⇄ worker（本机 `claude -p`），SSE 流式与轮询兜底、工具调用 / 结果 / thinking / 用量 / 额度事件、取消、超时、`/context` 构成与 `/compact`、独立运行（`claude-bridge serve / worker`）与嵌入现有 FastAPI 应用
