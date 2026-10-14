# Changelog

版本号遵循 semver；宿主按 tag 更新（安装方式见 README「安装」）。

## v0.10.0 — 2026-10-14

- **当前用户与用量统一放在右上角**：Chat、Code 项目页、进入项目后的会话页，右上角都显示「名字 · 5 小时 / 本周用量 · 管理」（此前 Chat 和项目会话页只在侧栏底部，侧栏收起就看不到）；手机上仍在侧栏抽屉底部
- **Chat / Code 切换改用 @szyyw/design 的 Squish Switch**：「Chat ⟨开关⟩ Code」，两侧文字也可点，支持拖动；开关滑到位后再切换内容
- **切换时侧栏不再整块消失**：Code 的项目选择页也用聊天页的骨架（侧栏列出项目，主区是项目卡片与新建表单），沿用同一份侧栏开关与宽度偏好；切换 Chat / Code / 打开或退出项目用 View Transition，侧栏框与顶栏不动，侧栏列表和主区内容按方向滑动淡入（减少动态效果时不做动画）
- 组件新增 `topEnd` 插槽（顶栏最右端）

## v0.9.2 — 2026-10-14

- 聊天页顶栏与首页顶栏按右上角工具位的实测高度加高（`--corner-rail-h` + 上下各 10px），悬浮按钮落在顶栏内居中，不再压住顶栏底边线。

## v0.9.1 — 2026-10-14

- **聊天页接入站点背景主题**：`app.html` / `index.html` 补上 `.bg-layer`，外观面板恢复「主题」一栏，6 个背景主题可切换、参数可调。
- 聊天页样式统一到站点风格（`bridge-pages.css`，全部限定在 `body.bp-chat` 下）：页面底透明；侧栏、顶栏、输入框改用设计包毛玻璃 token（`--glass-bg` / `--glass-border` + backdrop-filter）；消息区透明，每一轮消息和空状态提示各自垫一层半透明底，保证各主题、明暗、配色下文字可读；手机端侧栏抽屉保持不透明。
- 嵌入组件 `bridge-widget.css` 未改动，缺省外观不变。

## v0.9.0 — 2026-10-14

- **消息区状态可视化**（`bridge-widget.js/.css`；效果参照 React Bits 的 Call Chip / Status Mark / Lattice Loader，MIT + Commons Clause，用 CSS 与内联 SVG 重写，不依赖动画库与图标包）：
  - 工具调用行（含工具组）：运行中底色按计时逐渐填满（按工具类型有预期时长，可用 `expected` / 组件选项 `toolExpected` 覆盖），显示实时用时；结束变绿并收起填充，失败抖一下变红；展开后的命令 / 输出、失败 / 无结果、「执行中 · n/m」「执行了 N 条命令」等原有信息与交互不变
  - 步骤、工具组、思考块前的状态图标取代闪烁圆点：空心圈 → 进度弧 → 勾 / 叉
  - 每轮回答顶部和等待态用方格波浪加实时秒表，完成后定格为「已思考 12.3s」（中断则为「已中断，用时 …」）。时长优先取 `usage` 事件的 `duration_ms`，其次服务端事件 / 消息时间戳，再次客户端首帧到完成的差值；刷新页面后已完成的轮次仍显示，数据里没有时长则不显示
  - 计时与 `tickJobs` 合并：只有存在运行中的项且页面可见时才刷新；`prefers-reduced-motion` 下无动画，计时照常
  - 新导出 `messageStrings(lang)`（`zh` / `ja` / `en`）、`fmtDur`、`callProgress`、`turnTiming`、`latticeHtml`、`markHtml`、`TOOL_EXPECTED_MS`；`renderTurn` / `mountBridgeWidget` 新增 `lang`、`expected` / `toolExpected` 选项，`strings` 新增 `thought`、`stopped`
  - 样式见 `bridge-widget.css` 末尾 `message-status` 段；旧的 `.bridge-run` 圆点与 `.bridge-wait::after` 闪烁圆点规则已删除（`@keyframes bridge-blink` 仍用于流式输出末尾的光标）
- **输入框换成 Prompt Bar**（移植自 React Bits，MIT + Commons Clause；`motion` 改为 CSS 过渡与 requestAnimationFrame，图标为 Hugeicons free 的内联 SVG）：随内容长高到 `maxRows` 行；发送键在回答进行中变形为停止键（原来的独立停止按钮 `.bridge-stop` 及其样式移除，`.bridge-send` 的旧样式由 `.bridge-pb .bridge-send` 取代）；底栏的模型 / 思考深度两个原生下拉框换成菜单与分档滑块（数据与分组同 `modelOptionsHtml`，「默认」单列一个按钮），最高档时输入框出现光点；窄屏下思考深度只显示图标（不再整个隐藏）
- **`/` 命令菜单**：`/new`、`/compact`（菜单内二次确认）、`/context`、`/model`、`/effort`，宿主可用 `commands` 追加或传 `false` 关闭
- **`@` 来源菜单**：宿主传入 `sources`（数组或按查询返回的函数）后启用，缺省不出现；＋ 按钮在有来源时打开「图片 + 来源」菜单，否则直接选图
- **语音输入（Voice Pill）**：点按切换 / 按住说话 / 左滑取消，胶囊里有实时音量条（`getUserMedia` + `AnalyserNode`）与计时；转写用 Web Speech API，中间结果实时写入、结束后定稿，语言跟随新选项 `locale`；浏览器不支持时不显示，权限被拒时提示；`voice: false` 关闭，`voice.transcribe` 接自定义转写
- 新导出：`parseToken`、`modelMenuRows`、`modelLabel`、`effortState`、`composerCommands`、`filterCommands`、`filterSources`、`morphPath`、`cubicBezier`、`mountVoicePill`、`startDictation`、`speechLang`、`joinDictation`、`speechRecognitionCtor`、`simulatedLevel`、`micLevel`、`voiceClock`、`hugeicon`、`VOICE_DEFAULTS`、`VOICE_ERRORS`；组件返回值新增 `input`
- 新样式变量 `--bridge-spark`、`--bridge-menu`、`--bridge-send-bg` / `--bridge-send-fg`、`--bridge-voice-bg`；新结构类 `.bridge-pb*`、`.bridge-voice*`。已有初始化选项、回调、`.bridge-input` / `.bridge-form` / `.bridge-tray` 与全部已有导出不变

## v0.8.2 — 2026-10-14

- **站点页面标题换成新字样**：登录页（口令 / 账户两种模式）与 403 页的「claude-bridge」改用设计包 v0.18.0 的 `.app-title`（Geist 600、纯色、字标动效）；页面加载的 @szyyw/design 升到 v0.18.0

## v0.8.1 — 2026-10-14

- **门户登录模式下账户只在门户里建**：开启门户 SSO 后，管理页的「新建用户」改为打开门户（按钮「在门户新建用户」），用户列表上方说明账户由门户统一管理；`POST /api/admin/users` 与 `POST /api/admin/users/{uid}/password` 返回 403。此前在这里建的账户只存在于本地，门卫只放行门户账户，所以永远登不上；门户账户第一次登录时仍会自动创建或认领同名的本地行
- **修复 macOS 上客户端工具的 schema 校验总是失败**：校验子进程设置 384 MB 地址空间上限时，macOS 内核拒绝内存类 rlimit（EINVAL），子进程直接退出，所有带客户端工具的请求和工具调用都被当作「校验超出预算」拒绝。macOS 上改为不设内存上限，仍保留 2 秒 CPU 上限、进程超时与输入/并发限制；Linux 等其他 POSIX 平台照旧设内存上限，设置失败仍拒绝

## v0.8.0 — 2026-10-11

- **界面只显示额度百分比**：管理页、账户页、用户列表不再出现美元；「100% ≈ $X」的手填输入框移除（`PUT /api/admin/quota` 的 `cap_*_usd` 仍可用，管理页会提示并可一键改回自动标定）
- **用户能看清自己用了多少**：`/api/me` 的每个窗口新增 `account_pct`（整个账户的使用率）、`others_pct`（其他 bridge 用户）、`outside_pct`（bridge 之外，如管理员自己的客户端、claude.ai）；账户页显示「你在 5 小时 / 本周额度里用了 x%」及这三者的拆分，顶栏总是显示自己的占比
- **每个对话占本周额度**：`GET /api/threads` 的每项新增 `quota_pct`（该对话累计占本周额度的百分比，未标定时为 null），对话列表显示「周额度 x%」；新导出 `quotaText`
- 本机用量按模型分开记录：上报的每分钟合计新增 `model`（规范化的模型 id），表 `bridge_local_usage` 换成 `bridge_machine_usage`（主键含模型；worker 重启后会重发最近 8 天）
- `BridgeConfig.cap_override` / `BridgeService.effective_cap()`：手动值优先、否则用自动标定值；`fmtPct` 对 0.1% 以下显示 `<0.1%`

## v0.7.0 — 2026-10-11

- **用量记账改为按 token 计价**：CLI 的 `total_cost_usd` 在续接会话时往往是整个会话的累计值，按轮记账会把之前的花费重复计入。现在每轮按 `usage` 里的输入 / 输出 / 缓存读 / 缓存写（区分 5 分钟与 1 小时）乘 API 标价计算（新模块 `pricing.py`），compact 同理。已有数据库首次启动时用库里的 usage 事件重算历史账本（meta `usage_repriced`）
- **「100% ≈ $X」自动标定**：
  - worker 新增 `--scan-local-usage`（`CLAUDE_BRIDGE_SCAN_LOCAL_USAGE=1`）：增量读取本机 `~/.claude/projects/**/*.jsonl`，按 `message.id` 去重，按分钟和来源（bridge 自己的会话 / 本机其他会话）汇总后上报 `POST /api/agent/local-usage`（新模块 `local_usage.py`）
  - 服务端保存账户使用率的历史样本（`bridge_util_samples`；首次启动时从已有 `rate_limit` 事件回填），与本机用量对齐估计每个窗口的容量（新模块 `calibration.py`）
  - 管理员不填时自动使用估值；手填值优先
- 账户状态新增字段：`machine_local_usd`、`machine_bridge_usd`、`estimate_range_usd`、`estimate_groups`、`effective_cap_usd`、`cap_source`、`bridge_pct`、`local_pct`、`external_pct`；`estimate_cap_usd` 的含义改为自动标定值。个人份额按 `effective_cap_usd` 换算
- 管理页：账户用量下显示 bridge / 本机其他 / 其他设备的占比；容量输入框留空即用自动值；文案统一为「API 标价等价」
- `bridge_usage` 新增 `cache_read_tokens`、`cache_write_tokens` 列

## v0.6.3 — 2026-10-10

- **站点页面改从 CDN 加载 @szyyw/design，不再 vendor**：删除 `static/vendor/szyyw-design/`（并 gitignore），`pyproject.toml` 的 package-data 不再打包它——Python 包（宿主嵌入用的 `bridge-widget.*`）从此不含设计包文件，嵌入场景本来就不加载设计包，不受影响。页面（登录 / 账户 / 管理 / 查看用户 / Code 项目 / 403 / 单密码模式）从 `https://design.szyyw.xyz/<DESIGN_VERSION>/` 加载 `tokens.css` + `components.css`，并加 import map `"@szyyw/design/" → DESIGN_BASE`，`corner-boot.js` / `bridge-pages.js` 改为 `import … from '@szyyw/design/…'`。版本号只在 `standalone.py` 的 `DESIGN_VERSION`，`render_page` 填 `{{design_base}}` / `{{design_origin}}` 占位符；环境变量 `DESIGN_BASE` 可覆盖（本地离线开发指向本机静态服务）
- 设计包升到 v0.15.0（新增的 TechText 未启用）
- `scripts/update-design.sh [tag]` 改为「确认 CDN 上有该版本 → 改 `DESIGN_VERSION`」，不再调上游 `sync.sh`；`upgrade-shared.sh` / bot workflow 随之改读写 `standalone.py`

## v0.6.2 — 2026-10-10

- 修复侧边栏会话列表被压扁：会话一多，`.bridge-threads`（flex 列 + `overflow:auto`）把每条 `.bridge-thread`（自带 `overflow:hidden`，最小高度因此为 0）按比例压缩，标题下沿被裁、条目重叠。改为 `.bridge-thread { flex-shrink: 0 }`，列表改为滚动
- 版本号补齐：v0.6.1 时 `_version.py` / `pyproject.toml` 仍是 0.6.0，本版一并改为 0.6.2

## v0.6.1 — 2026-10-09

- 设计包升到 v0.14.2（毛玻璃 backdrop-filter 书写顺序修正、全局 `[hidden]` 隐藏）；删掉 `bridge-pages.css` 里重复的 `[hidden]` 兜底。组件（`bridge-widget.*`）无改动

## v0.6.0 — 2026-10-09

- **组件里不再弹浏览器原生对话框**（对齐 @szyyw/design DESIGN §8，但不依赖设计包、只用 `--bridge-*` 着色）：
  - 删除对话：⋯ 菜单里首点「删除对话」变红成「再点一次确认删除」并在菜单里展开影响说明（菜单保持打开），5 秒内再点才删；关菜单 / 点别处即取消
  - 压缩会话（`/context` 面板，`renderContextPanel`）：首点变成「再点一次确认压缩」并在面板里说明后果，再点才调 `onCompact`
  - 重命名：组件自带的输入弹层（`role="dialog"` + `aria-modal`、标题做 label、焦点进输入框并全选、Tab 不出框、Enter 保存、Esc / 点外 / ✕ / 取消关闭、关闭后焦点回到 ⋯ 按钮；手机上是底部抽屉；`prefers-reduced-motion` 不做动画）
- 新导出（对外 API，故为 minor）：`armConfirm` / `disarmConfirm` / `promptDialog`，宿主自己的外壳（如线程菜单）可以直接用。新文案键 `strings.delArm / renameLabel / save / cancel`
- 新样式类：`.bridge-armed`、`.bridge-menu-warn`、`.bridge-ctx-warn`、`.bridge-dlg*`（z-index 70，挂在 body 下、带 `bridge-root`，宿主祖先的 transform / backdrop-filter 困不住它）

## v0.5.1 — 2026-10-09

- **剩下的页面也统一到 @szyyw/design**：「账号未开通」403 页（原来是内联样式的两行字）改成模板 `static/denied.html`（`.glass.panel.empty` + 回到门户按钮 + 工具位，SSO 时带账户菜单可切换账号）；standalone 单密码模式的登录页与聊天页加载 `tokens.css` + `components.css` + `corner-boot.js`（只有 🌗 / 外观，`portal: null`），服务端同样按 `cb_*` cookie 渲染外观属性；聊天页右上角原来 fixed 的「退出」挪到侧栏底部，不再和工具位重叠
- **破坏性操作按 DESIGN §8 二次确认**，替代浏览器原生 `confirm()` / `prompt()`：管理页「重置密码」「停用账户」首点变红「再点一次确认」并展开影响说明（5 秒内再点才执行）；「删除用户」与 Code 页「删除项目」弹出 `.sheet`，输入用户名 / 项目名后删除按钮才可用（语义同前：输入名字才能删）。`bridge-pages.js` 新导出 `armConfirm` / `disarm` / `confirmTyped`；叠在编辑弹层上的确认弹层按 Esc 只关最上面那层
- 嵌入组件（`bridge-widget.*`）无改动

## v0.5.0 — 2026-10-09

- **嵌入组件跟随宿主的 @szyyw/design**：`bridge-widget.css` 的 `--bridge-*` 缺省值改为 `var(--设计包 token, 回退值)`（`--bg / --sheet-bg / --text / --text-dim / --glass-border / --hover-tint / --accent / --on-accent / --err / --on-err / --ok / --overlay-bg / --term-* / --pop-shadow`），明暗改用 `light-dark()` 跟随宿主 `color-scheme`，去掉 `prefers-color-scheme` 媒体查询——宿主固定深色 / 浅色、换配色时组件即时跟随，宿主不必再映射 token 或重写 `.bridge-*` 皮肤。`/context` 构成的分类色改用 `--bridge-chart-1…6`（缺省 `--chart-1…6`），不再写死 hex。按钮 / 角标 / 遮罩 / 缩略图上的 `#fff`、`rgba(0,0,0,…)` 收进 token（新增 `--bridge-err-fg` / `--bridge-scrim` / `--bridge-term-border` / `--bridge-on-image-bg|fg`）；下拉箭头改用 `currentColor`。`--bridge-font` 缺省 `inherit`（跟宿主字体）。`--bridge-*` 覆写接口不变、原有变量名全部保留。行为变化：宿主根上没有 `color-scheme` 时组件是浅色（之前跟随系统），要跟随系统请在宿主设 `color-scheme: light dark`
- 组件修：主列与侧栏收起时的网格列改为 `minmax(0, 1fr)`，窄屏顶栏的长状态文字不再把整页撑出横向滚动；偏好面板的行是 `<label>`，宿主全局 `label { display: block; margin; color }`（如 components.css）不再改到它们
- **多用户页面统一到 @szyyw/design v0.13.1**（登录 / 账户 / 管理 / 查看用户 / Code 项目页）：直接加载 `tokens.css` + `components.css`，自写的 `.bp-card / .bp-field / .bp-input / .bp-actions / .bp-tag / .bp-bar / .bp-err / .bp-blocked / .bp-dialog / .bp-head / .au-tabs`、页面 toast 换成包的 `.glass.panel / .form-row + .field / .actions / .pill / .bar / .err-text / .callout / .overlay + .sheet / .app-header / .tabs / .seg / .tbl / toast()`；账户 / 管理页加点阵背景。`corner-boot.js` 改用 `mountChrome`（🌗 + 外观弹层总是挂，SSO 时再挂应用切换器与账户菜单），不再 fetch `components.css` 包 `@scope`；顶栏给工具位让位改用 `--corner-rail-w`（不再写死 100px / 88px）
- **外观首屏**：服务端读 `cb_theme / cb_palette / cb_scheme` cookie 渲染 `<html data-theme / data-palette / data-scheme>`（DESIGN §2.1），取代写死的 `data-scheme="auto"`；默认同设计包（深色）
- 修：管理页「新建用户」弹层里露出了只属于编辑的「重置密码 / 停用 / 删除」按钮（只隐藏了第一个编辑专用块）
- `scripts/update-design.sh` 从目标 tag 取 `sync.sh`（之前取 main）

- **IP 归属地**：管理页用户行的最近访问、`/admin/users/<id>` 的访问记录，IP 下面多一行「国家 省 市 · 运营商」（同门户首页公网 IP 卡片）。服务端 `ipgeo.py` 查 ip-api.com batch（zh-CN），按 IP 永久缓存在新表 `bridge_ipgeo`；tailnet 显示「Tailscale 内网」、其他内网 / 保留地址显示「内网」，不出网；查询失败不缓存、不影响页面。接口 `last_access.geo`、访问记录每行 `geo`（`{country, region, city, isp}` / `{label}` / `null`），仅管理员接口
- **站点 favicon**：独立站点（`serve` 单密码 / 多用户）根路径提供 `/favicon.svg`、`/favicon.ico`、`/apple-touch-icon.png`（文件在 `static/site/`，来自门户的品牌素材，应用层路由不要求登录），全部 HTML 页面 head 带三个 `<link>`。嵌入式组件（`create_bridge` + `bridge-widget.*`）不注册这些路由、不碰宿主的 favicon
- 部署方式调整：服务端改为从 git 仓库构建镜像部署，健康检查失败自动回滚
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
- 容器部署新增 `SZYYW_SSO` / `SZYYW_SSO_AUTOCREATE` / `PORTAL_ORIGIN` 三个环境变量（默认全关）；`szyyw-auth` 进 `[serve]` 依赖（worker 与嵌入式宿主不需要）

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
- 部署：`Dockerfile`；`deploy/mac/` 的 LaunchAgent 与安装脚本；通用模板移到 `deploy/examples/`

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
