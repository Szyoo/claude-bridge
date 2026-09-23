# claude-bridge

把「网页聊天框」接到「某台机器上登录好的 `claude` 命令行」:

```
浏览器 ──SSE/REST──▶ 服务端（FastAPI + SQLite 任务队列） ◀──长轮询/POST── worker（本机 `claude -p`）
```

- 服务端不跑模型、不需要任何 Anthropic 凭证,只做信箱:收消息、排任务、把 worker 回传的增量推给浏览器。
- worker 在有 `claude` 登录态的机器上跑,领任务 → `claude -p --output-format stream-json --include-partial-messages` → 把文本增量、工具调用 / 结果、thinking、用量、额度事件批量回传;支持中途取消、超时、进程组回收、SIGTERM 收尾。
- 浏览器端是无框架的 ES module 客户端(`bridge-client.js`,SSE + 轮询兜底)和一个可直接用的参考组件(`bridge-widget.js/.css`)。
- 既能**独立运行**(`claude-bridge serve` + `claude-bridge worker`),也能**嵌入**现有 FastAPI 应用(两个 router + 一套 hooks)。

## 安装

不在 PyPI，按 tag 装（`vX.Y.Z` 见 [tags](https://github.com/Szyoo/claude-bridge/tags) / [CHANGELOG](CHANGELOG.md)）：

```bash
pip install "claude-bridge @ https://github.com/Szyoo/claude-bridge/archive/refs/tags/v0.2.0.tar.gz"
pip install "claude-bridge[serve] @ https://github.com/Szyoo/claude-bridge/archive/refs/tags/v0.2.0.tar.gz"   # 独立运行还需要 uvicorn
```

或者**把 `src/ pyproject.toml README.md CHANGELOG.md LICENSE` 拷进宿主仓库**（vendoring）（例如 `packages/claude-bridge/`，只读），用一个更新脚本从 tag 覆盖，再 `pip install -e packages/claude-bridge`——宿主的构建和部署就不需要访问 GitHub。

依赖:`fastapi`、`pydantic>=2`、`requests`。Python ≥ 3.11。**单进程**部署(`Broker` 在内存里,多 worker 进程会让订阅者收不到发布)。

## 独立运行

```bash
# 服务端(任何有公网 / 内网可达的机器)
CLAUDE_BRIDGE_PASSWORD=口令 CLAUDE_BRIDGE_SECRET=随机串 CLAUDE_BRIDGE_AGENT_TOKEN=共享令牌 \
CLAUDE_BRIDGE_DB=/data/bridge.db claude-bridge serve --host 0.0.0.0 --port 8770

# worker(装了 claude 并 `claude login` 过的机器)
CLAUDE_BRIDGE_URL=https://bridge.example.com CLAUDE_BRIDGE_AGENT_TOKEN=共享令牌 \
CLAUDE_BRIDGE_CWD=/path/to/project CLAUDE_BRIDGE_ALLOWED_TOOLS="Read,Glob,Grep,Bash(git log *)" \
claude-bridge worker

claude-bridge status    # 服务端可达性 + claude --version + claude auth status
```

| 变量 | 用途 | 默认 |
|---|---|---|
| `CLAUDE_BRIDGE_DB` | SQLite 文件 | `claude-bridge.db` |
| `CLAUDE_BRIDGE_FILES` | 聊天里上传的图片存放目录 | DB 同目录下的 `claude-bridge-files/` |
| `CLAUDE_BRIDGE_PASSWORD` / `_SECRET` | 登录口令 / cookie 签名密钥(不设则每次重启失效) | — |
| `CLAUDE_BRIDGE_AGENT_TOKEN` | 服务端与 worker 的共享 Bearer 令牌 | — |
| `CLAUDE_BRIDGE_COOKIE_SECURE` | `1`/`0`;默认非 localhost 为 1 | — |
| `CLAUDE_BRIDGE_URL` | worker 连的服务端地址 | — |
| `CLAUDE_BRIDGE_CWD` | `claude` 的工作目录 | 当前目录 |
| `CLAUDE_BRIDGE_ALLOWED_TOOLS` | 逗号分隔的 `--allowedTools` | 空(不加参数) |
| `CLAUDE_BRIDGE_SYSTEM_PROMPT` / `_FILE` | `--append-system-prompt` | — |
| `CLAUDE_BRIDGE_MODEL` / `_MAX_TURNS` / `_PERMISSION_MODE` / `_CHAT_TIMEOUT` / `_CLAUDE_BIN` | 同名 CLI 参数 | `""` / 40 / — / 900 / `claude` |

命令行 flag 覆盖环境变量;`worker --once` 只处理一个任务就退出(调试用),`serve --no-auth` 关掉登录(本地调试)。

## 嵌入现有 FastAPI 应用

```python
from claude_bridge import BridgeConfig, BridgeStore, create_bridge, static_dir

store = BridgeStore(conn=my_sqlite_conn, lock=my_rlock)      # 或 BridgeStore("bridge.db")
bridge = create_bridge(
    store=store,
    config=BridgeConfig(scopes=("stock", "fund"), new_thread_notice="新对话已开始",
                        model_choices=[{"id": "", "label": "默认"}, {"id": "sonnet", "label": "Sonnet"}],
                        on_thread_deleted=lambda tid: cleanup(tid)),
    browser_auth=my_cookie_dependency,       # FastAPI 依赖;None = 不鉴权
    agent_auth=my_bearer_dependency,         # 或传 agent_token=... 让包自己生成
)
bridge.mount(app, browser_prefix="/api/bridge", agent_prefix="/api/agent", static_prefix="/static/bridge")
```

宿主可以直接用 `bridge.service`(`start_chat / cancel / find_thread / settings / requeue_stale …`)和 `bridge.store`(`enqueue_job / recent_jobs …`)——任务表是通用的,宿主自己的任务类型(比如「跑一遍复盘」)也走同一个队列,由 worker 的 `handlers` 处理。

worker 侧嵌入:

```python
from claude_bridge.client import BridgeClient
from claude_bridge.worker import Hooks, Worker, WorkerConfig

class MyHooks(Hooks):
    def build_context(self, *, thread_id, is_new_session, payload):   # 新会话自动拼的【背景】
        return "【背景】...\n\n【用户消息】\n" if is_new_session else ""
    def env(self, payload): return {"MY_TOKEN": "..."}                  # 注给 claude 子进程的环境变量
    def on_chat_finished(self, job, text, summary): notify(text)      # 完成回调(summary 含 usage / 费用 / 额度)
    def tick(self): scheduler.maybe_run()                              # 每次领任务前

worker = Worker(BridgeClient(url, token),
                WorkerConfig(cwd=REPO, allowed_tools=[...], system_prompt=SP, model="sonnet"),
                hooks=MyHooks(), handlers={"review": run_review})     # handler(job, worker) -> dict|str|None
worker.run_forever()
```

## 浏览器端

```html
<script type="module">
  import { BridgeClient } from '/static/bridge/bridge-client.js';
  import { mountBridgeWidget } from '/static/bridge/bridge-widget.js';
  const client = new BridgeClient({ baseUrl: '/api/bridge', onAuthLost: () => location.href = '/login' });
  mountBridgeWidget(document.getElementById('chat'), client, { scope: 'stock', markdown: myMarkdownFn });
</script>
```

只要传输层时用 `client.subscribe(threadId, handlers)`:`onSnapshot / onMessage / onDelta / onEvent / onStatus / onDone / onThread / onContext / onJob / onFallback / onError`。增量按 `rev` 去重(重复跳过、断档自动重拉整条);EventSource 连续失败会降级为轮询,30 秒后再尝试 SSE;401 触发 `onAuthLost`。组件的样式全部通过 `--bridge-*` 自定义属性覆盖,markdown 渲染优先用 `opts.markdown`,其次 `window.marked`(先转义),否则纯文本。

界面照 Claude Code 客户端:助手消息通栏无容器、用户消息浅底块、工具调用折叠成一行灰字(连续多条合并为「执行了 N 条命令」)、正文与工具组**按真实顺序交错**(每个 `tool_use` / `thinking` 事件带 `at` = 当时已输出的正文字数,在下一个段落边界切开,不会切进代码块)。宿主想自己排版但复用这套渲染时,`bridge-widget.js` 还导出:

| 导出 | 用途 |
|---|---|
| `splitTurn(content, events)` | 纯函数:一条回答 → `[{kind:'text'}, {kind:'steps', items}]` 交错段 |
| `renderTurn(el, message, {markdown, head, timestamps, open})` | 把一条消息渲染进 `el`(结构类名 `bridge-turn / bridge-md / bridge-steps / bridge-step / bridge-term`,重渲保留用户展开状态) |
| `renderSteps / stepsHtml` | 只渲染工具组 |
| `sessionSummary(messages, {context})` | 模型 / 上下文占用(`pct`)/ 本轮调用与 tokens / 5h·7d 额度 |
| `renderSessionPanel(el, summary, {onRefresh, onCompact, busy})` · `renderContextPanel` | 会话弹层内容(含 `/context` 构成) |
| `DEFAULT_PREFS / loadPrefs / savePrefs / sanitizePrefs / applyPrefs(root, prefs)` | 每台设备自己的界面偏好(文字大小、密度三档 13/14/16px、正文宽度、发送键、时间戳、工具默认展开、代码换行、侧栏、聊天区高度、拖过的输入框高 / 侧栏宽),`applyPrefs` 只写 CSS 变量与 class |
| `renderPrefsPanel(el, prefs, {onChange, classes, fields})` | 偏好控件;`classes` 可把结构类名映射到宿主设计系统的开关 / 胶囊 |
| `isSendKey(e, prefs) / sendHint(prefs) / placePopover(anchor, pop) / attachDrag(handle, onMove, onEnd) / fmtTime / fmtRelative` | 发送键判定、fixed 弹层定位(窄屏由样式改成底部抽屉)、拖拽改高 / 改宽 |
| `planImage / prepareImages / mountAttachments({button, input, tray, textarea, dropZone, client})` | 发图:见下 |

### 图片

宿主在 `BridgeConfig(files_dir=...)` 里给一个目录就开启上传(`None` = 关闭,`GET /settings` 的 `uploads.enabled` 会告诉前端)。

- **浏览器**:`mountAttachments` 接管 📎 按钮 / 输入框粘贴 / 拖入,选好就处理并上传,托盘显示缩略图;发送时 `client.send(text, {files: att.ids()})`,有图时文字可以为空。处理只管尺寸不压画质:长边 > 2000px 才等比缩(同一请求图超过 20 张时 API 要求每张 ≤2000px,CLI 每轮都会重发历史里的图),长截图(长宽比 > 2.4)不缩、切成 ≤2000px 的段按顺序发;PNG 截图保持 PNG,相册照片(JPEG / HEIC)出 JPEG。
- **服务端**:按文件头魔数认 PNG / JPEG / GIF / WebP(不信任 Content-Type,不收 SVG),单张默认 ≤7MB(API 单图 10MB 上限是按 base64 算的);文件落盘 `<files_dir>/<id>.<ext>`,表 `bridge_files` 记元数据;发送时绑定到用户消息,删对话一起删,24 小时没发出去的上传自动清掉。
- **worker**:任务 payload 带 `files` 时,从 `GET /api/agent/files/{id}` 取回、base64,改用 `claude -p --input-format stream-json` 把图片和文字放进同一条用户消息(图在前,多张时逐张标注);不带图的消息命令行不变。

### 模型列表

worker 启动后在后台用本地、零费用的 `claude -p "/model" --no-session-persistence` 探测本机 CLI:别名(`opus` / `sonnet` / `haiku` / `fable` / `opus[1m]`…,各自指向这版 CLI 的最新模型)和宿主 `model_choices` 里哪些固定版本本机认得(`/model <id>` 会回 "not found"),`POST /api/agent/models` 上报;之后每 6 小时、以及 `claude --version` 变化时重探。`GET /settings` 的 `models` 优先用上报(带 `group`:「跟随 CLI 最新」/「固定版本」,前端用 `modelOptionsHtml` 渲染成 `<optgroup>`),没上报时退回 `model_choices`;`models_info.source` 说明来源。`WorkerConfig(model_probe=False)` 可关掉。

## HTTP API

浏览器 router(宿主决定前缀):

| 方法 路径 | 说明 |
|---|---|
| `GET /threads?scope=` · `POST /threads` · `GET /threads/find?scope=&key=` · `GET /threads/{id}` · `POST /threads/{id}/select` · `PATCH /threads/{id}` · `DELETE /threads/{id}` | 线程管理;`key` 是宿主自定义查找键(如「某天的复盘」) |
| `GET /threads/{id}/messages?after=&limit=&events=1` | 消息(含结构化事件);`after=0` 取最新 N 条 |
| `POST /send {text, scope?, key?, new_thread?}` · `POST /threads/{id}/messages {text}` | 发消息,同一线程有未完成回答时 409 |
| `GET /threads/{id}/stream` | SSE,见下 |
| `POST /messages/{id}/cancel` | `{status: cancelled \| cancelling \| noop}` |
| `POST /threads/{id}/context` · `POST /threads/{id}/compact` | 排一个 worker 任务：`claude -p "/context"`（本地计算、零费用）刷新这段会话的上下文构成 / `claude -p "/compact"` 压缩历史；结果存在线程上（`thread.context`），并以 SSE `context` / `job` 帧推给浏览器。每次回答结束 worker 也会自动刷新一次构成 |
| `POST /files?name=` · `GET /files/{id}` | 上传图片(请求体就是图片本身)/ 取图;发送时 `/send` 与 `/threads/{id}/messages` 带 `files: [id…]` |
| `GET/PUT /settings` · `GET /jobs` · `GET /jobs/{id}` · `GET /status` | 模型 / effort / 轮数 / 是否带背景 / 上传开关;任务表;helper 在线状态 |

Agent router(Bearer 令牌):`POST /jobs/next`(长轮询)· `GET /jobs/{id}` · `GET /files/{id}` · `POST /jobs/{id}/events {status?, deltas?, events?}` → `{ok, cancel}` · `POST /jobs/{id}/finish {ok, cancelled?, result?, error?, error_kind?, session_id?, reset_session?}` · `POST /chat` · `GET /status`。

SSE 帧:`snapshot`(首帧:线程(含 `context` 构成)+ 消息 + 进行中的消息 id + helper 状态 + 事件游标)、`message`、`delta {message_id, text, rev}`、`event`(`init | tool_use | tool_result | thinking | usage | rate_limit | compact | status | error`)、`status`、`thread`、`context`、`job`、`done`;每 15 s 一个 `: ping`。`id:` 是事件表的全局自增 id,浏览器重连时带 `Last-Event-ID` 只补新事件。

## 数据表

`bridge_threads / bridge_messages / bridge_events / bridge_jobs / bridge_meta`,建表幂等,可与宿主共用一个 SQLite 文件(共享连接时不改任何 PRAGMA 和 `row_factory`)。心跳超过 `stale_seconds`(默认 120 s)的运行中任务会被判失败并把关联消息置为 `error`,不会出现永远「正在回答」的线程。

## 测试

```bash
pip install -e ".[dev,serve]"
pytest -q && ruff check src tests      # 浏览器端的纯函数测试也在里面（node --test，需要 Node ≥ 18）
```

GitHub Actions 在每次 push / PR 上跑同样的检查（Python 3.11 / 3.12）。

## 发版流程

1. 改 `src/claude_bridge/_version.py` 与 `pyproject.toml` 里的版本号，`CHANGELOG.md` 写上这一版
2. 提交，打 tag：`git tag vX.Y.Z && git push && git push --tags`
3. 等 Actions 绿

## 宿主如何更新

- **拷贝方式**（推荐）：宿主里放一个 `update-bridge.sh`，默认拉 GitHub 最新 tag 覆盖副本、`--local` 从本机 clone 同步（联调没发版的改动）；随后 `pip install -e <副本>`、跑宿主测试、部署服务端，**再重启 worker**（worker 用的是同一份代码）
- **pip 方式**：把 tag 地址里的版本号改掉重新安装
- 网页上可以提示更新：`GET /settings` 的 `bridge` 给出服务端版本、worker 最近一次上报的版本（不一致 = worker 还没重启），前端用 `checkBridgeUpdate({current})`（`bridge-client.js`）比对 GitHub 最新 tag，结果在 localStorage 缓存 6 小时

`tests/` 用一个可执行的假 `claude` 脚本回放真实 stream-json 形态,覆盖增量去重、工具配对、取消(响应标记 / 轮询两条路径)、超时、SIGTERM、坏会话重置、SSE 快照与重连、独立模式登录。
