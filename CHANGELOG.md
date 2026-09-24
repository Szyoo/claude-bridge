# Changelog

版本号遵循 semver；宿主按 tag 更新（见 README「宿主如何更新」）。

## v0.3.3 — 2026-09-24

- **Code 模式按项目工作**：每人一片独立空间，项目 = 其中的一个目录（通常是 git 仓库）。切到 Code 先选项目；没有项目时引导「克隆 Git 仓库」（https / ssh / git@）或「新建空项目」（`git init`），完成后自动打开；顶栏显示当前项目，点它回到项目页。每个项目有自己的对话列表（scope `code:<项目>`），Claude 在项目目录里读写文件、跑命令
- 服务端 `bridge_projects` 表 + `GET/POST/DELETE /projects`（`BridgeConfig(projects=True)`）；新建 / 克隆 / 删除作为 `project` 任务交给 worker 执行（带心跳，不弹凭据输入，失败时清理半成品目录）；`BridgeConfig.scopes` 可以是 `(scope, owner) -> bool`
- worker profile 的 `cwd` 支持 `{project}`（scope `<profile>:<项目>`）；启动时创建基础工作目录

## v0.3.2 — 2026-09-24

- **Chat / Code 两种模式**：页面顶栏切换，各自一套对话列表（scope `""` / `code`）；任务带上 thread 的 `scope` 与 `owner`
- worker **profiles**（`--profiles` / `CLAUDE_BRIDGE_PROFILES`，JSON `{scope: 覆盖项}`）：按 scope 换工作目录（`{owner}` → 每人一个）、`--tools`、`--allowedTools`、权限模式、`--settings`、`--strict-mcp-config`、系统提示词；`/context` `/compact` 在同一目录 `--resume`。`deploy/mac/profiles.json`：Chat 只有联网搜索，Code 是一套编程工具 + `bypassPermissions`，两者都不加载 MCP
- 手机上侧栏抽屉能收回了（点露出的遮罩或 Esc）；组件 `destroy()` 会移除容器上的监听（同一容器重新挂载不再重复响应）；新增 `topStart` 插槽

## v0.3.1 — 2026-09-24

- `--env-file PATH`：先从 KEY=VALUE 文件读 `CLAUDE_BRIDGE_*`（已设置的环境变量优先），令牌不必写进 plist / unit 文件
- 部署：`Dockerfile` + `deploy/vps/`（compose 只挂 ingress 网络、显式项目名）+ `scripts/deploy-vps.sh`；`deploy/mac/` 的 LaunchAgent 与安装脚本；通用模板移到 `deploy/examples/`

## v0.3.0 — 2026-09-24

- **多用户模式** `serve --multi-user`：用户名 + 密码登录（PBKDF2），365 天自动续期的登录态，改密码 / 重置 / 停用即刻让其它设备登出；每人的线程、上传、当前对话、设置互相隔离；`/admin` 分发账户（自动生成初始密码）、改角色、停用、删除；`/account` 自助改用户名 / 显示名 / 密码、看用量；`claude-bridge users add|list|passwd|enable`；部署模板 `deploy/`（systemd / Caddy / nginx / launchd）
- **配额按订阅额度的百分比**：每轮等价费用记入 `bridge_usage` 账本，按账户真实的 5 小时 / 每周窗口累计，经管理员的「100% ≈ $X」换算成百分比，按人设上限；账户整体用量到保护线时暂停普通用户；worker 每 10 分钟零费用 `claude -p "/usage"` 上报账户用量（`POST /api/agent/limits`），`/compact` 的费用也记账
- 核心库：`Principal`（`browser_auth` 可返回它来按 owner 隔离）、`BridgeConfig.check_quota`、`QuotaExceeded`；组件新增 `fill`（铺满整页，隐藏聊天区高度偏好）与 `sideFoot`（侧栏底部插槽）

## v0.2.0 — 2026-09-24

- **发图片**：`POST /files`（请求体即图片，按魔数认 PNG / JPEG / GIF / WebP）、发送时 `files: [id]` 绑定到用户消息；worker 遇到带图的消息改用 `claude -p --input-format stream-json` 把图片直接放进用户消息。浏览器端 `planImage / prepareImages / mountAttachments`：只做尺寸上限不压画质（长边 > 2000px 等比缩，长截图切段）
- **模型列表按 worker 本机 CLI 动态上报**：零费用的 `claude -p "/model" --no-session-persistence` 探测别名（跟随 CLI 最新）与宿主候选的固定版本；启动时、每 6 小时、CLI 版本变化时重探；`GET /settings` 返回分组列表，前端 `modelOptionsHtml`
- **压缩会话看得到进度**：session 任务排队 / 开始 / 结束 / 心跳超时都推 `job` 帧，快照带进行中的任务，完成后在对话里留一行；`/compact` 运行中每 15 秒心跳（修复超过 120 秒被判超时）
- **界面照 Claude Code 客户端重做**：通栏轮次、工具调用折叠成一行并与正文按真实顺序交错（`splitTurn`）、每台设备自己的界面偏好（`renderPrefsPanel`）、会话信息收进底部弹层、手机上的抽屉 / 遮罩 / 背景锁定
- **版本**：`claude_bridge.__version__` / `REPO`；`GET /settings`、`GET /status` 带 `bridge: {version, repo, helper_version}`；浏览器 `checkBridgeUpdate` 查 GitHub 最新 tag

## v0.1.0 — 2026-09-22

- 首版：浏览器 ⇄ 服务端（FastAPI + SQLite 任务队列）⇄ worker（本机 `claude -p`），SSE 流式与轮询兜底、工具调用 / 结果 / thinking / 用量 / 额度事件、取消、超时、`/context` 构成与 `/compact`、独立运行（`claude-bridge serve / worker`）与嵌入现有 FastAPI 应用
