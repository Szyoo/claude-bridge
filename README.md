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

```bash
pip install -e packages/claude-bridge            # 库 + CLI
pip install -e "packages/claude-bridge[serve]"   # 独立运行还需要 uvicorn / python-multipart
```

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

只要传输层时用 `client.subscribe(threadId, handlers)`:`onSnapshot / onMessage / onDelta / onEvent / onStatus / onDone / onThread / onFallback / onError`。增量按 `rev` 去重(重复跳过、断档自动重拉整条);EventSource 连续失败会降级为轮询,30 秒后再尝试 SSE;401 触发 `onAuthLost`。组件的样式全部通过 `--bridge-*` 自定义属性覆盖,markdown 渲染优先用 `opts.markdown`,其次 `window.marked`(先转义),否则纯文本。

## HTTP API

浏览器 router(宿主决定前缀):

| 方法 路径 | 说明 |
|---|---|
| `GET /threads?scope=` · `POST /threads` · `GET /threads/find?scope=&key=` · `GET /threads/{id}` · `POST /threads/{id}/select` · `PATCH /threads/{id}` · `DELETE /threads/{id}` | 线程管理;`key` 是宿主自定义查找键(如「某天的复盘」) |
| `GET /threads/{id}/messages?after=&limit=&events=1` | 消息(含结构化事件);`after=0` 取最新 N 条 |
| `POST /send {text, scope?, key?, new_thread?}` · `POST /threads/{id}/messages {text}` | 发消息,同一线程有未完成回答时 409 |
| `GET /threads/{id}/stream` | SSE,见下 |
| `POST /messages/{id}/cancel` | `{status: cancelled \| cancelling \| noop}` |
| `GET/PUT /settings` · `GET /jobs` · `GET /jobs/{id}` · `GET /status` | 模型 / effort / 轮数 / 是否带背景;任务表;helper 在线状态 |

Agent router(Bearer 令牌):`POST /jobs/next`(长轮询)· `GET /jobs/{id}` · `POST /jobs/{id}/events {status?, deltas?, events?}` → `{ok, cancel}` · `POST /jobs/{id}/finish {ok, cancelled?, result?, error?, error_kind?, session_id?, reset_session?}` · `POST /chat` · `GET /status`。

SSE 帧:`snapshot`(首帧:线程 + 消息 + 进行中的消息 id + helper 状态 + 事件游标)、`message`、`delta {message_id, text, rev}`、`event`(`init | tool_use | tool_result | thinking | usage | rate_limit | status | error`)、`status`、`thread`、`done`;每 15 s 一个 `: ping`。`id:` 是事件表的全局自增 id,浏览器重连时带 `Last-Event-ID` 只补新事件。

## 数据表

`bridge_threads / bridge_messages / bridge_events / bridge_jobs / bridge_meta`,建表幂等,可与宿主共用一个 SQLite 文件(共享连接时不改任何 PRAGMA 和 `row_factory`)。心跳超过 `stale_seconds`(默认 120 s)的运行中任务会被判失败并把关联消息置为 `error`,不会出现永远「正在回答」的线程。

## 测试

```bash
cd packages/claude-bridge && pytest -q && ruff check src tests
```

`tests/` 用一个可执行的假 `claude` 脚本回放真实 stream-json 形态,覆盖增量去重、工具配对、取消(响应标记 / 轮询两条路径)、超时、SIGTERM、坏会话重置、SSE 快照与重连、独立模式登录。
