# claude-bridge

把网页聊天接到一台已登录 `claude` 命令行的机器上：

```
浏览器 ──SSE/REST──▶ 服务端（FastAPI + SQLite 任务队列） ◀──长轮询/POST── worker（`claude -p`）
```

- **服务端**不跑模型、不需要 Anthropic 凭证：收消息、排任务，把 worker 回传的增量推给浏览器。
- **worker** 在有 `claude` 登录态的机器上运行：领任务 → `claude -p --output-format stream-json` → 批量回传文本增量、工具调用与结果、thinking、用量和额度事件；支持取消、超时、进程组回收。
- **浏览器端**是无框架的 ES module：客户端 `bridge-client.js`（SSE，失败时降级为轮询）和可直接使用的聊天组件 `bridge-widget.js/.css`。
- 可以**独立运行**（`claude-bridge serve` + `claude-bridge worker`），也可以**嵌入**已有的 FastAPI 应用。

## 功能

- 流式回复；工具调用折叠显示，并按真实顺序与正文交错
- 回答进行中可以接着发：消息先排队，Claude 在下一步（工具调用之间）读到并插进当前回答；也可以「立即发送」打断当前回答马上回复
- 发送图片（浏览器端按尺寸处理后上传，worker 以 stream-json 输入交给 Claude）
- 模型列表由 worker 按本机 CLI 探测上报，CLI 更新后自动跟上
- 会话上下文构成（`/context`）与压缩（`/compact`）
- 多用户模式：账户、按用户隔离的对话与设置、按订阅额度比例的配额
- Chat / Code 两种模式：Code 按项目工作（克隆仓库或新建空项目），worker 按模式使用不同的工具与权限
- 可选：完整内容流与调用方工具，见 [docs/caller-tools-and-content.md](docs/caller-tools-and-content.md)
- 服务端程序的一次性任务：调用方令牌 + `/api/caller/*`（见下文「调用方（服务端程序）」）

## 安装

```bash
pip install "claude-bridge[serve] @ https://github.com/Szyoo/claude-bridge/archive/refs/tags/vX.Y.Z.tar.gz"
```

只运行 worker 或只嵌入组件时不需要 `[serve]`。版本见 [tags](https://github.com/Szyoo/claude-bridge/tags) 与 [CHANGELOG](CHANGELOG.md)。需要 Python ≥ 3.11。服务端需单进程部署（推送依赖进程内的 `Broker`）。

## 独立运行

```bash
# 服务端
CLAUDE_BRIDGE_PASSWORD=口令 CLAUDE_BRIDGE_AGENT_TOKEN=共享令牌 \
claude-bridge serve --host 0.0.0.0 --port 8770

# worker（已执行过 claude login 的机器）
CLAUDE_BRIDGE_URL=https://bridge.example.com CLAUDE_BRIDGE_AGENT_TOKEN=共享令牌 \
CLAUDE_BRIDGE_CWD=/path/to/workdir claude-bridge worker

claude-bridge status    # 检查服务端连通性与本机 claude 登录状态
```

| 变量 | 用途 |
|---|---|
| `CLAUDE_BRIDGE_HOST` / `_PORT` | 服务端监听地址（缺省 `127.0.0.1:8770`） |
| `CLAUDE_BRIDGE_DB` / `_FILES` | SQLite 文件 / 上传图片目录 |
| `CLAUDE_BRIDGE_PASSWORD` / `_SECRET` / `_COOKIE_SECURE` | 单口令登录 / cookie 签名密钥 / cookie 的 Secure 属性（缺省：非本机监听时开启） |
| `CLAUDE_BRIDGE_MULTI_USER` / `_TZ` | `1` = 多用户模式 / 配额提示里重置时间的时区 |
| `CLAUDE_BRIDGE_SCOPES` | 多用户模式下额外接受的 scope，逗号分隔（默认只接受空 scope 与项目 scope）。给有自己 worker profile 的外部客户端用，例如浏览器扩展 |
| `CLAUDE_BRIDGE_AGENT_TOKEN` | 服务端与 worker 的共享令牌 |
| `CLAUDE_BRIDGE_URL` / `_CWD` / `_WORKER_NAME` | worker 连接的服务端 / `claude` 的工作目录 / worker 名称 |
| `CLAUDE_BRIDGE_CLAUDE_BIN` / `_MODEL` / `_MAX_TURNS` | `claude` 可执行文件 / 缺省模型 / 最大轮数 |
| `CLAUDE_BRIDGE_IDLE_TIMEOUT` / `_CHAT_TIMEOUT` | 一轮对话连续多少秒没有任何输出（文字、思考、工具调用或结果）就停止（缺省 1200）/ 一轮最长运行秒数（缺省 7200）；`0` = 不限。profile 里可用 `idle_timeout` / `chat_timeout` 按模式覆盖 |
| `CLAUDE_BRIDGE_ALLOWED_TOOLS` / `_PERMISSION_MODE` / `_SYSTEM_PROMPT` / `_SYSTEM_PROMPT_FILE` | 对应的 `claude` 参数 |
| `CLAUDE_BRIDGE_PROFILES` | 按模式（scope）覆盖工作目录、工具、权限的 JSON 文件 |
| `CLAUDE_BRIDGE_BACKENDS` | 其他后端（Anthropic 兼容接口，如 DeepSeek）的私有 JSON 文件，只给指定管理员的网页端用，见下方「其他后端」 |
| `CLAUDE_BRIDGE_SCAN_LOCAL_USAGE` | `1` = worker 上报本机所有 Claude Code 会话的用量（读 `~/.claude/projects` 的 transcript，只发送每分钟的 token 合计与 API 标价等价，不含内容），用于自动标定额度 |
| `CLAUDE_BRIDGE_CONCURRENCY` | worker 同时执行的任务数（缺省 1）；多用户模式下管理员可在「管理 › helper」覆盖，约 30 秒生效 |
| `CLAUDE_BRIDGE_CLIENT_TOOLS` / `_CONTENT_BLOCKS` | `1` = 启用调用方工具 / 完整内容流（见 [docs/caller-tools-and-content.md](docs/caller-tools-and-content.md)） |

命令行参数优先于环境变量；`--env-file PATH` 可从文件读入变量。部署示例见 [deploy/](deploy/)。

### 其他后端

worker 可以给指定的管理员一个「Claude | DeepSeek」切换（侧栏最上方）（任何提供 Anthropic 兼容接口的模型都行）。在 worker 机器上建一个私有文件（`chmod 600`），用 `CLAUDE_BRIDGE_BACKENDS` 指向它，格式见 [deploy/mac/backends.example.json](deploy/mac/backends.example.json)：

- `owners`：可以使用的账户 id；还要求该账户当前是 admin。普通用户、API 令牌和调用方（callers）都用不了
- `models`：切到这个后端时模型菜单里的选项，`id` 即传给 `--model` 的模型名，第一个为默认
- `env`：这些对话里 `claude` 的环境变量（`ANTHROPIC_BASE_URL` 等）；worker 自身的 Claude 凭据（`CLAUDE_CODE_OAUTH_TOKEN` 等）不会带进这些对话
- API key：两种放法。一是 owners 在网页「我的账户」里填写：保存在服务端，worker 每 30 秒同步配置时取走，页面上只显示末 4 位；二是写在 `env` 的 `ANTHROPIC_AUTH_TOKEN` 里：只留在 worker 机器上，不上报服务端，此时以它为准、网页不显示填写框。两处都没有 key 时，发送会提示先去填写
- `pricing` / `peak` / `currency`：按对方 API 价格记账用（每百万 token 的价格，币种由 `currency` 指定，缺省 USD，DeepSeek 用 CNY；按模型名匹配；`peak` 为高峰时段与倍数）。改了币种后，已记的账会按记下的 token 数和高峰标记重新计价
- `balance_url`：可选，worker 每 10 分钟用 key 查一次账户余额，显示在页面上
- `projects_root`：可选，例如 `~/Documents/GitHub`。设了以后这个后端的 Code 项目就是该目录下的文件夹（worker 每 30 秒上报一次列表），新建和克隆都放进去，网页上不删除；不设则和 Claude 共用每个账户隔离的项目目录
- `local`：可选，`true` 表示模型跑在 worker 这台机器上（如 Ollama）：不计价、不要 API key、页面不显示余额与金额，改为显示 token 用量、输出速度（输出 token ÷ 模型用时）和本机状态（内存余量、swap、模型服务当前加载的模型与上下文，worker 每次配置同步时上报，约 30 秒一次）
- `profile`：可选，这个后端的对话专用的运行参数，键与 `CLAUDE_BRIDGE_PROFILES` 相同（不含 `cwd`，目录仍由 scope 决定），叠加在 scope 的 profile 之上，例如更长的 `chat_timeout` / `idle_timeout` 或它自己的 `system_prompt`

**本地模型（Ollama）**：Ollama 自带 Anthropic 兼容接口（`/v1/messages`，含 thinking 与工具调用），`ANTHROPIC_BASE_URL` 指向 `http://127.0.0.1:11434` 即可，`ANTHROPIC_AUTH_TOKEN` 随便填；示例见 `backends.example.json` 的 `local`。模型名不在 Claude Code 的已知列表里时它只打警告，照常工作；`ANTHROPIC_DEFAULT_*_MODEL` 与 `CLAUDE_CODE_SUBAGENT_MODEL` 都要指向本地模型，否则子任务和后台请求会去找不存在的 Claude 模型。小上下文的本地模型要省着用：
- `profile.tools` 只留需要的内置工具、`strict_mcp: true` 不带本机 MCP 配置——完整工具集光系统提示就要 8k～13k token，精简后约 5k；
- 模型的 `num_ctx`（Modelfile 里 `PARAMETER num_ctx`）与 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 设成一样，超出前由 Claude Code 自己压缩，而不是被服务端从头截断（截掉的是系统提示）；
- `permission_mode` 用默认值时，网页对话里写文件和跑命令都会被拒绝（没有人在终端里点允许）；
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`：不然每轮答完 Claude Code 还会再发一个数千 token 的后台请求，本地模型要多算二三十秒，还会挤掉主对话的缓存；`CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` / `BASH_MAX_OUTPUT_LENGTH` 限制一次读文件和命令输出的大小；
- `models[].context`：模型的上下文窗口（token），页面的上下文占用按它计算；
- 内存吃紧的机器上，Ollama 服务开 `OLLAMA_FLASH_ATTENTION=1`、`OLLAMA_KV_CACHE_TYPE=q8_0`（KV 缓存减半）、`OLLAMA_NUM_PARALLEL=1`（并行数会成倍占 KV 缓存）、`OLLAMA_MAX_LOADED_MODELS=1`、`OLLAMA_KEEP_ALIVE=10m`（空闲卸载）；它内部 llama-server 的内存提示词缓存默认可涨到 8 GiB，可用 `LLAMA_ARG_CACHE_RAM=<MiB>` 限制。

切到这个后端后，对话列表、模型菜单和设置都是它自己的（对话的 scope 是 `<原 scope>@<后端名>`，Code 模式下用同一个项目目录）。它的用量单独记账，按对方的 API 价格估算（区分缓存命中、未命中和高峰时段；节假日不识别，按高峰价估，宁高勿低），显示在 DeepSeek 一侧的账户行与它自己的账户页（`/account?svc=<后端名>`），不计入 Claude 额度，也不参与额度标定。

### Docker

仓库根目录的 `Dockerfile` 构建服务端镜像（多用户模式，数据在 `/data` 卷，端口 8770，健康检查 `/api/health`）。建议放在能终止 HTTPS 的反向代理之后，不要直接发布端口：

```bash
docker build -t claude-bridge .
docker run -d --name claude-bridge --restart unless-stopped \
  -e CLAUDE_BRIDGE_AGENT_TOKEN=共享令牌 \
  -e CLAUDE_BRIDGE_COOKIE_SECURE=1 \
  -v claude-bridge-data:/data \
  claude-bridge
```

worker 不放进镜像：它需要在已执行过 `claude login` 的机器上运行。

## 多用户模式

```bash
claude-bridge users --db bridge.db add <用户名> --admin   # 先建管理员
claude-bridge serve --multi-user --db bridge.db
```

用户名 + 密码登录；每个人的对话、上传和设置互相隔离。管理员在 `/admin` 管理账户与配额，用户在 `/account` 修改资料和密码、查看用量。配额按每轮实际的 token 数（按模型与输入 / 输出 / 缓存加权）换算为 5 小时 / 每周额度的百分比，换算比例由账户使用率与 worker 本机用量自动标定（需 `CLAUDE_BRIDGE_SCAN_LOCAL_USAGE=1`）。用户在 `/account` 能看到自己用了多少，以及账户总用量里其他用户、bridge 之外各占多少；对话列表显示每个对话占本周额度的比例。另可设置整体用量阈值。`users` 还有 `list` / `passwd` / `enable` / `map` / `unmap` 子命令。

**门户 SSO**（`SZYYW_SSO=1`，或 `CLAUDE_BRIDGE_SSO=1`）：浏览器身份只取前置门卫注入的 `X-User` / `X-Role` / `X-Portal-Sub`，本地密码登录与会话 cookie 不再生效；只能部署在会剥掉客户端同名头的门卫之后，且不发布端口。`X-Portal-Sub` 对应本地账户行（`users map <用户名> <portal_sub>` 手动绑定；未绑定时按同名用户名认领一次），`X-Role` 逐请求决定管理权限。`SZYYW_SSO_AUTOCREATE=1` 时自动为新门户用户建账户；`PORTAL_ORIGIN` 指定登录 / 登出跳转的门户（缺省 `https://szyyw.xyz`）。`/api/agent/*`（Bearer）与 `/api/health` 不受影响。

## 调用方（服务端程序）

给不经浏览器的后端程序用：每个调用方一枚独立的 Bearer 令牌（与 worker 的 `CLAUDE_BRIDGE_AGENT_TOKEN` 无关，打不开 `/api/agent/*` 与浏览器接口），绑定一个账户和允许的 scope。任务以该账户身份运行（配额检查、用量都记在它名下），但每个任务都是独立的一次性对话：不续接会话、worker 端不保留 CLI 会话（`--no-session-persistence`），不出现在该账户的聊天列表里，结束 1 小时后删除（用量记录保留）。

```bash
# 服务端（多用户模式；令牌只显示一次，库里只存 SHA-256）
claude-bridge callers --db bridge.db add myapp --user <用户名> --scope task [--max-inflight 2] [--token-only]
claude-bridge callers --db bridge.db list | rotate <名称> | disable <名称> | enable <名称>
```

scope 需要服务端接受（多用户模式下加进 `CLAUDE_BRIDGE_SCOPES`），worker 的 `CLAUDE_BRIDGE_PROFILES` 里给它一个 profile（例如 `"task": {"cwd": "~/work/task/{owner}", "tools": [], "strict_mcp": true}`：没有任何工具）。

| 路由（`/api/caller` 前缀） | 说明 |
|---|---|
| `POST /tasks` | `{text, scope, options?, timeout?=120, require_online?=true}`；`options` 同「逐轮选项」（`model` / `instructions` / `max_turns` / `max_output_tokens` / `thinking` / `effort`）。返回 `{task_id, status:"queued"}` |
| `GET /tasks/{id}?wait=0..25` | 长轮询，返回 `{status, text, error, error_kind, usage, model, elapsed_ms}`；`status` ∈ `queued / running / done / failed / cancelled`，`text` 只在 `done` 时给出 |
| `POST /tasks/{id}/cancel` | 取消 |
| `GET /models` · `GET /status` | worker 上报的模型列表 / worker 在线与队列 |

失败时 `error_kind` ∈ `offline`（worker 不在线，提交时直接 503）、`timeout`（超过 `timeout` 秒，自动取消；排队时间也算）、`quota`（账户配额或订阅额度不足）、`busy`（同时进行的任务超过上限，429）、`cancelled`、`claude`、`worker`；提交被拒时另有 `scope` / `owner`（403）、`invalid`（400）。

## Messages 兼容接口（API 模式）

服务端和 worker 都开启调用方工具（`CLAUDE_BRIDGE_CLIENT_TOOLS=1`）后，`/v1/messages`、`/v1/messages/count_tokens`、`/v1/models` 按 Anthropic Messages API 的格式工作（流式与非流式、工具调用、思考块）。支持自定义 API 地址的客户端把地址设为站点根、密钥设为 scope 含 `api` 的调用方令牌即可：

```bash
ANTHROPIC_BASE_URL=https://bridge.example.com
ANTHROPIC_API_KEY=cbc_…        # 多用户模式下可在 /account 自助创建；也可 callers add … --scope api
```

请求以令牌所属账户的身份运行（配额与用量记在其名下）。请求里的 `system` 替换 CLI 自带的系统提示；请求里的 `tools` 注册为调用方工具，由客户端执行，worker 上不开放内置工具。对话按历史指纹续接同一个 CLI 会话（保住提示缓存），无法识别的历史会以文本形式带入新会话。worker 需要一个 `api` profile（见 `deploy/mac/profiles.json`），建议 `--concurrency` 设为 2–3。细节与限制见 [docs/api-mode.md](docs/api-mode.md)。

## 嵌入 FastAPI 应用

```python
from claude_bridge import BridgeConfig, BridgeStore, create_bridge

bridge = create_bridge(
    store=BridgeStore("bridge.db"),
    config=BridgeConfig(new_thread_notice="新对话已开始"),
    browser_auth=my_auth_dependency,     # 返回 claude_bridge.Principal 即可按用户隔离
    agent_token="共享令牌",
)
bridge.mount(app, browser_prefix="/api/bridge", agent_prefix="/api/agent", static_prefix="/static/bridge")
```

worker 侧可以通过 `Hooks`（拼接上下文、注入环境变量、完成回调）和 `handlers`（自定义任务类型）扩展：

```python
from claude_bridge.client import BridgeClient
from claude_bridge.worker import Worker, WorkerConfig

Worker(BridgeClient(url, token), WorkerConfig(cwd=REPO, model="sonnet"), hooks=MyHooks()).run_forever()
```

## 浏览器端

```html
<script type="module">
  import { BridgeClient } from '/static/bridge/bridge-client.js';
  import { mountBridgeWidget } from '/static/bridge/bridge-widget.js';
  const client = new BridgeClient({ baseUrl: '/api/bridge', onAuthLost: () => location.href = '/login' });
  mountBridgeWidget(document.getElementById('chat'), client, { scope: '' });
</script>
```

只用传输层时调用 `client.subscribe(threadId, handlers)`。

回答进行中继续发送：`client.send(text, { thread, queue: true })` 在对话忙时不再返回 409，而是排队（返回 `{ queued, thread }`）。worker 把排队的消息写进正在运行的 `claude` 的输入，Claude 在下一步读到时，这条消息以 `user_note` 事件出现在当前回答里（组件渲染为「你插话」）；当前回答结束时还没读到的，自动作为下一轮发出。`client.removeQueued(thread, id)` 撤回还没交给 Claude 的一条，`client.sendQueueNow(thread)` 停止当前回答（已做的保留）并马上回复排队的消息。排队状态在快照的 `queue` 和 SSE 的 `queue` 帧（`onQueue` 回调）里。组件自带的输入框已接好：回答中输入内容时发送键变为「排队发送」，输入框为空时是「停止」。自己写输入框的宿主用 `mountQueue(el, client, { thread: () => id, onRestore(text, files) })` 显示排队区，把快照的 `queue` 和每个 `onQueue` 帧交给它的 `update(items, busy)`。

一次性调用（阅后即焚）：`client.send(text, { scope, ephemeral: true })` 每次新建一个隐藏对话，不续接会话，worker 端不保留 CLI 会话（`--no-session-persistence`），不出现在对话列表里，也不会变成当前对话。拿到结果后调用 `client.deleteThread(thread, scope)` 立即删除；没删的在最后一次活动 1 小时后（`BridgeConfig.ephemeral_seconds`）由服务端自动清掉。适合翻译、抽取、识图这类每次独立的任务。组件样式通过 `--bridge-*` CSS 变量覆盖；`bridge-widget.js` 还导出 `renderTurn`、`splitTurn` 等渲染函数，供宿主自行排版。

### 输入框（Prompt Bar）与语音输入（Voice Pill）

组件的输入框随内容自动长高（到 `maxRows` 行或可见高度的 40% 后在框内滚动，不能手动拖高），发送按钮在回答进行中变形为停止键。底栏依次是：设置、＋（选图片，或在有来源时打开来源菜单）、模型与思考深度选择器（数据同 ⚙ 里的下拉框：「跟随 CLI 最新 / 固定版本」分组，helper 不认的已存值标注后保留）、上下文胶囊、麦克风、发送 / 停止。图片照旧可粘贴、拖放、选择，长截图自动切片。Enter 发送 / Shift+Enter 换行（或按偏好 ⌘/Ctrl+Enter），输入法组合中不发送。

- 输入框开头敲 `/` 弹出命令：`/new`（新对话）、`/compact`（压缩会话，菜单里再确认一次）、`/context`（上下文构成与额度）、`/model`、`/effort`；↑↓ 选择，Enter / Tab 执行，Esc 关闭。
- 敲 `@` 弹出来源菜单——组件本身没有可引用的来源，缺省不出现；宿主传入 `sources` 后才启用。
- 麦克风：点一下开始 / 再点结束，按住超过 300ms 则松开即结束，按住时向左滑取消（恢复原文）；胶囊里显示实时音量条与计时。转写用浏览器的 Web Speech API（`SpeechRecognition` / `webkitSpeechRecognition`），说话时中间结果实时写入输入框，结束后定稿；音量条用 `getUserMedia` + `AnalyserNode`。浏览器不支持语音识别时不显示麦克风；麦克风权限被拒时给出提示。识别语言跟随 `locale`。

| 选项 | 缺省 | 说明 |
|---|---|---|
| `maxRows` | `12` | 输入框自动长高的最大行数（同时不超过视口 40%） |
| `locale` | 挂载点最近的 `lang`，否则浏览器语言 | 决定语音识别语言：`zh*` → `zh-CN`，`ja*` → `ja-JP`，`en*` → `en-US`，其他原样 |
| `commands` | `[]` | 追加的 `/` 命令：`[{ name: '/report', description, run({ widget, thread, input, toast }) }]`；传 `false` 关闭 `/` 菜单 |
| `sources` | `null` | `@` 来源：数组 `[{ key, name, description?, icon?, insert?, onPick? }]`，或函数 `(query) => 数组或 Promise`。`icon` 为内置图标名（`file` / `image`）或一段 SVG；选中后把 `insert`（缺省 `@名称 `）插入输入框并调用 `onPick(source)` |
| `onSource` | `null` | 来源未带 `onPick` 时的统一回调 `(source) => …` |
| `openImage` | `null` | 点开消息里的图片：`(items, start, link) => …`，`items` 为该条消息全部图片 `[{ src, alt }]`。用设计包时传 `(items, start, link) => openLightbox({ items, start, returnFocus: link })`（`@szyyw/design/lightbox.js`，v0.31.0+）；不传则在新标签页打开原图。自己调 `renderTurn` 渲染消息的宿主，对消息列表调用 `enhanceTurnImages(listEl, openImage)`（返回解绑函数） |
| `voice` | `true` | `false` 关闭语音；或对象 `{ lang, transcribe, mode, holdAfter, slideToCancel, cancelDistance, showTime, waveform, reactive, size, shape, reach, attack, release, sensitivity, floor, openDuration, pressScale, accentColor, iconColor, background, ariaLabel, onStart, onStop, onResult }` |

自定义转写：`voice.transcribe({ lang, stream, signal, onPartial })` 返回 `Promise<string>`（最终文本）。`stream` 是 `Promise<MediaStream | null>`（组件为音量条打开的麦克风流），`onPartial(text)` 把中间结果写进输入框，用户结束时 `signal` 中止（`signal.reason === 'cancel'` 表示取消、结果丢弃）。提供 `transcribe` 时即使浏览器没有 Web Speech 也显示麦克风。

```js
mountBridgeWidget(el, client, {
  locale: 'ja-JP',
  sources: [{ key: 'spec', name: '设计稿', description: '本周的设计文档' }],
  onSource: (s) => console.log('引用', s.key),
  voice: { mode: 'toggle', transcribe: async ({ stream, signal, onPartial }) => myAsr(await stream, signal, onPartial) },
});
```

样式变量新增 `--bridge-spark`（思考深度最高档时的光点，缺省 `--bridge-accent`）、`--bridge-menu`（菜单底色）、`--bridge-send-bg` / `--bridge-send-fg`（发送键）、`--bridge-voice-bg`（语音胶囊）。拆开用时可直接调用导出的 `mountVoicePill(button, opts)` 与 `startDictation(opts)`。

输入框与语音胶囊移植自 [React Bits](https://reactbits.dev) 的 Prompt Bar 与 Voice Pill（MIT + Commons Clause），图标路径来自 [Hugeicons](https://hugeicons.com) free（MIT）。

## HTTP API

浏览器路由（前缀由宿主决定）：

| 路径 | 说明 |
|---|---|
| `GET/POST /threads` · `GET/PATCH/DELETE /threads/{id}` | 对话管理 |
| `GET /threads/{id}/messages` · `GET /threads/{id}/stream` | 消息 / SSE 推送 |
| `POST /send` · `POST /threads/{id}/messages` | 发送消息（`/send` 带 `ephemeral: true` 为一次性调用，见上文） |
| `POST /messages/{id}/cancel` | 取消回答 |
| `DELETE /threads/{id}/queue/{qid}` · `POST /threads/{id}/queue/send-now` | 撤回排队的消息 / 打断当前回答并马上回复排队的消息（发送时带 `queue: true` 才会排队） |
| `GET /threads/find?scope=&key=` · `POST /threads/{id}/select` | 按 scope + key 找对话 / 设为当前对话 |
| `POST /threads/{id}/context` · `POST /threads/{id}/compact` | 上下文构成 / 压缩会话 |
| `POST /files` · `GET /files/{id}` | 上传 / 读取图片 |
| `GET/POST /projects` · `DELETE /projects/{name}` | Code 模式的项目（克隆或新建） |
| `GET /jobs` · `GET /jobs/{id}` | 任务状态 |
| `GET /threads/{id}/client-tools` · `POST /messages/{id}/client-tools/{call_id}/result` | 调用方工具（启用时） |
| `GET/PUT /settings` · `GET /status` | 设置 / 状态 |

worker 路由使用 Bearer 令牌：`POST /jobs/next`（长轮询）、`GET /jobs/{id}`、`POST /jobs/{id}/events`、`POST /jobs/{id}/inbox`（取回答进行中排队的消息）、`POST /jobs/{id}/finish`、`POST /chat`（持令牌的后端直接发起对话）、`GET /status`、`GET /files/{id}`、`POST /limits`（额度上报）、`POST /local-usage`（本机用量的每分钟合计）、`GET/POST /models`（模型列表）。

调用方路由（`/api/caller/*`）使用调用方令牌，见上文「调用方（服务端程序）」。

独立运行时另有 `/login`、`/logout`、`/api/health`；多用户模式另有 `/api/me`、`/account`、`/admin` 与 `/api/admin/*`。

## 开发

```bash
pip install -e ".[dev,serve]"
pytest -q && ruff check src tests
```

## 许可

MIT，见 [LICENSE](LICENSE)。
