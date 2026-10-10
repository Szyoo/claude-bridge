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
- 发送图片（浏览器端按尺寸处理后上传，worker 以 stream-json 输入交给 Claude）
- 模型列表由 worker 按本机 CLI 探测上报，CLI 更新后自动跟上
- 会话上下文构成（`/context`）与压缩（`/compact`）
- 多用户模式：账户、按用户隔离的对话与设置、按订阅额度比例的配额
- Chat / Code 两种模式：Code 按项目工作（克隆仓库或新建空项目），worker 按模式使用不同的工具与权限
- 可选：完整内容流与调用方工具，见 [docs/caller-tools-and-content.md](docs/caller-tools-and-content.md)

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
| `CLAUDE_BRIDGE_AGENT_TOKEN` | 服务端与 worker 的共享令牌 |
| `CLAUDE_BRIDGE_URL` / `_CWD` / `_WORKER_NAME` | worker 连接的服务端 / `claude` 的工作目录 / worker 名称 |
| `CLAUDE_BRIDGE_CLAUDE_BIN` / `_MODEL` / `_MAX_TURNS` / `_CHAT_TIMEOUT` | `claude` 可执行文件 / 缺省模型 / 最大轮数 / 单次超时秒数 |
| `CLAUDE_BRIDGE_ALLOWED_TOOLS` / `_PERMISSION_MODE` / `_SYSTEM_PROMPT` / `_SYSTEM_PROMPT_FILE` | 对应的 `claude` 参数 |
| `CLAUDE_BRIDGE_PROFILES` | 按模式（scope）覆盖工作目录、工具、权限的 JSON 文件 |
| `CLAUDE_BRIDGE_SCAN_LOCAL_USAGE` | `1` = worker 上报本机所有 Claude Code 会话的用量（读 `~/.claude/projects` 的 transcript，只发送每分钟的 token 合计与 API 标价等价，不含内容），用于自动标定额度 |
| `CLAUDE_BRIDGE_CLIENT_TOOLS` / `_CONTENT_BLOCKS` | `1` = 启用调用方工具 / 完整内容流（见 [docs/caller-tools-and-content.md](docs/caller-tools-and-content.md)） |

命令行参数优先于环境变量；`--env-file PATH` 可从文件读入变量。部署示例见 [deploy/](deploy/)。

## 多用户模式

```bash
claude-bridge users --db bridge.db add <用户名> --admin   # 先建管理员
claude-bridge serve --multi-user --db bridge.db
```

用户名 + 密码登录；每个人的对话、上传和设置互相隔离。管理员在 `/admin` 管理账户与配额，用户在 `/account` 修改资料和密码、查看用量。配额按每轮实际的 token 数（按模型与输入 / 输出 / 缓存加权）换算为 5 小时 / 每周额度的百分比，换算比例由账户使用率与 worker 本机用量自动标定（需 `CLAUDE_BRIDGE_SCAN_LOCAL_USAGE=1`）。用户在 `/account` 能看到自己用了多少，以及账户总用量里其他用户、bridge 之外各占多少；对话列表显示每个对话占本周额度的比例。另可设置整体用量阈值。`users` 还有 `list` / `passwd` / `enable` / `map` / `unmap` 子命令。

**门户 SSO**（`SZYYW_SSO=1`，或 `CLAUDE_BRIDGE_SSO=1`）：浏览器身份只取前置门卫注入的 `X-User` / `X-Role` / `X-Portal-Sub`，本地密码登录与会话 cookie 不再生效；只能部署在会剥掉客户端同名头的门卫之后，且不发布端口。`X-Portal-Sub` 对应本地账户行（`users map <用户名> <portal_sub>` 手动绑定；未绑定时按同名用户名认领一次），`X-Role` 逐请求决定管理权限。`SZYYW_SSO_AUTOCREATE=1` 时自动为新门户用户建账户；`PORTAL_ORIGIN` 指定登录 / 登出跳转的门户（缺省 `https://szyyw.xyz`）。`/api/agent/*`（Bearer）与 `/api/health` 不受影响。

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

只用传输层时调用 `client.subscribe(threadId, handlers)`。组件样式通过 `--bridge-*` CSS 变量覆盖；`bridge-widget.js` 还导出 `renderTurn`、`splitTurn` 等渲染函数，供宿主自行排版。

## HTTP API

浏览器路由（前缀由宿主决定）：

| 路径 | 说明 |
|---|---|
| `GET/POST /threads` · `GET/PATCH/DELETE /threads/{id}` | 对话管理 |
| `GET /threads/{id}/messages` · `GET /threads/{id}/stream` | 消息 / SSE 推送 |
| `POST /send` · `POST /threads/{id}/messages` | 发送消息 |
| `POST /messages/{id}/cancel` | 取消回答 |
| `GET /threads/find?scope=&key=` · `POST /threads/{id}/select` | 按 scope + key 找对话 / 设为当前对话 |
| `POST /threads/{id}/context` · `POST /threads/{id}/compact` | 上下文构成 / 压缩会话 |
| `POST /files` · `GET /files/{id}` | 上传 / 读取图片 |
| `GET/POST /projects` · `DELETE /projects/{name}` | Code 模式的项目（克隆或新建） |
| `GET /jobs` · `GET /jobs/{id}` | 任务状态 |
| `GET /threads/{id}/client-tools` · `POST /messages/{id}/client-tools/{call_id}/result` | 调用方工具（启用时） |
| `GET/PUT /settings` · `GET /status` | 设置 / 状态 |

worker 路由使用 Bearer 令牌：`POST /jobs/next`（长轮询）、`GET /jobs/{id}`、`POST /jobs/{id}/events`、`POST /jobs/{id}/finish`、`POST /chat`（持令牌的后端直接发起对话）、`GET /status`、`GET /files/{id}`、`POST /limits`（额度上报）、`POST /local-usage`（本机用量的每分钟合计）、`GET/POST /models`（模型列表）。

独立运行时另有 `/login`、`/logout`、`/api/health`；多用户模式另有 `/api/me`、`/account`、`/admin` 与 `/api/admin/*`。

## 开发

```bash
pip install -e ".[dev,serve]"
pytest -q && ruff check src tests
```

## 许可

MIT，见 [LICENSE](LICENSE)。
