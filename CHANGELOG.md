# Changelog

版本号遵循 semver；宿主按 tag 更新（见 README「宿主如何更新」）。

## 未发布

- **IP 归属地**：管理页用户行的最近访问、`/admin/users/<id>` 的访问记录，IP 下面多一行「国家 省 市 · 运营商」（同门户首页公网 IP 卡片）。服务端 `ipgeo.py` 查 ip-api.com batch（zh-CN），按 IP 永久缓存在新表 `bridge_ipgeo`；tailnet 显示「Tailscale 内网」、其他内网 / 保留地址显示「内网」，不出网；查询失败不缓存、不影响页面。接口 `last_access.geo`、访问记录每行 `geo`（`{country, region, city, isp}` / `{label}` / `null`），仅管理员接口
- **站点 favicon**：独立站点（`serve` 单密码 / 多用户）根路径提供 `/favicon.svg`、`/favicon.ico`、`/apple-touch-icon.png`（文件在 `static/site/`，来自平台仓库 `branding/`，应用层路由不要求登录），全部 HTML 页面 head 带三个 `<link>`。嵌入式组件（`create_bridge` + `bridge-widget.*`）不注册这些路由、不碰宿主的 favicon
- **部署改为 git clone + 推送即上线**：VPS 的 `/opt/claude-bridge` 现在是 git clone，由平台的 `deploy-app.sh`（`szyyw-autodeploy` 每 10 分钟）部署 main，健康检查失败自动回滚。`scripts/deploy-vps.sh` 不再 rsync：检查在 main / 工作区干净 / 已推送后 ssh 触发立即部署，新增 `--rollback <ref>`、`--status`，去掉 `--dry-run`
- **自动升级共享包**：`scripts/upgrade-shared.sh` + `.github/workflows/upgrade-shared.yml`（每 6 小时 / 手动）检测 `szyyw-auth` 与 `@szyyw/design` 的上游最新正式 tag，升级（含 vendored 副本）后跑 ruff + pytest，通过才以 bot 身份推到 main；失败不推送

## v0.4.2 — 2026-10-05

- **管理员查看用户**：管理页的用户行显示最近一次访问的 IP · 设备，新增「查看」→ `/admin/users/<id>`：只读浏览该用户全部对话（Chat / Code·项目分组，消息含工具调用、图片）与访问记录。接口 `GET /api/admin/users/<id>/threads`、`…/threads/<tid>/messages`、`…/access`、`GET /api/admin/files/<id>`，全部只读：不改用户的当前对话、对话时间、最近在线，用户侧无任何痕迹
- **访问记录** `bridge_access`：每个已登录请求记 IP（Caddy 的 `X-Forwarded-For`）与 User-Agent，同一用户 + IP + 设备 30 分钟内的请求合并为一行（首次 / 末次 / 次数，每分钟最多写一次库）；本地密码模式另记登录与登录失败；保留 180 天，删用户时一起删
- 修：管理页编辑弹窗里点「重置密码 / 停用 / 删除」会先把弹窗重置成「新建用户」，导致这三个操作无效

## v0.4.1 — 2026-10-02

- **门户 SSO 下的右上角工具**：`serve --multi-user` 在 `SZYYW_SSO=1` 时，app / account / admin 页面加载 [@szyyw/design](https://github.com/Szyoo/szyyw-design) v0.8.0 的应用切换器（`mountAppSwitcher`）与账户菜单（`mountAccountMenu`）；`<html>` 上标 `data-sso="1" data-portal="<PORTAL_ORIGIN>" data-scheme="auto"`（服务端注入，模板不变）
- 设计包 vendor 到 `src/claude_bridge/static/vendor/szyyw-design/`（`scripts/update-design.sh [tag]`，委托上游 `sync.sh`），随 package-data 打包；`static/corner-boot.js` 负责挂载
- 样式隔离：`tokens.css` 只定义 `:root` 变量（bridge 用 `--bridge-*`，不冲突），直接 `<link>`；`components.css` 带全局规则（`*`、`body`、`a`、`.btn`…），由 `corner-boot.js` 取回后整份包进 `@scope (body) to (:scope > :not(.corner-tools, .app-switcher, .account-menu))` 作为 adopted stylesheet，只作用于工具位和两个面板；不支持 `@scope` 的浏览器不挂工具
- SSO 下页面里的「退出」按钮（侧栏 / 项目页顶栏、账户页）去掉：登出由账户菜单完成（门户登出）；`POST /logout` 仍保留（跳回门户）。用量 / 管理入口照旧。顶栏右侧让出工具位的宽度
- SSO 关闭时页面输出与 v0.4.0 完全相同；standalone 单密码模式与嵌入式组件（`create_bridge` + `bridge-widget.*`）不加载任何设计包文件

## v0.4.0 — 2026-10-01

- **门户 SSO**（`serve --multi-user`，`SZYYW_SSO=1` / 别名 `CLAUDE_BRIDGE_SSO=1`，默认关）：身份来自 `*.szyyw.xyz` Caddy 门卫注入的 `X-Portal-Sub` / `X-User` / `X-Role`（[szyyw-auth](https://github.com/Szyoo/szyyw-auth) v0.1.0 契约），cookie 不再认人；`/login` 跳门户（`PORTAL_ORIGIN`，默认 `https://szyyw.xyz`，带 `?rd=`），`/logout` 回门户，本地密码登录和改密码关闭；管理权限按每次请求的 `X-Role`，不改写存储的角色。未开启时行为不变
- `bridge_users` 新增可空唯一列 `portal_sub`（门户账号的固定 ID；门户用户名可改，所以不按名字存；启动时自动迁移，ids 与数据归属不变）。认人：先按 `portal_sub = X-Portal-Sub`，再按 `username = X-User` 且未映射的行（认领：填入 sub 并记日志），否则 `SZYYW_SSO_AUTOCREATE=1` 自动建号、默认 403「此账号尚未在 claude-bridge 开通」。缺 `X-Portal-Sub` = 未登录，不按用户名兜底。本地用户名不随门户改名；页面上显示本次请求的 `X-User`
- 设置映射：`claude-bridge users map <用户名> <门户ID>` / `users unmap <用户名>`，管理页「门户 ID」，`POST /api/admin/users/<id>/portal-user {"portal_sub": …}`；`/api/admin/users` 的每行多了 `portal_sub`
- `deploy/vps/compose.yml` 加 `SZYYW_SSO` / `SZYYW_SSO_AUTOCREATE` / `PORTAL_ORIGIN`（默认全关）；`szyyw-auth` 进 `[serve]` 依赖（worker 与嵌入式宿主不需要）

## v0.3.5 — 2026-10-01

- worker 每分钟（原来是 10 分钟）检查一次 `claude --version`，更新 CLI 后模型列表一两分钟内就换成新版本对应的模型；探测失败的重试仍是 10 分钟一次

## v0.3.4 — 2026-09-24

- Code 模式的侧栏是项目自己的：顶部显示当前项目（名称 · 分支 · 来源）和「切换」，列表叫「会话」（＋ 新会话 / 删除会话），空白页提示在哪个项目里开始；组件新增 `sideHead` 插槽

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
