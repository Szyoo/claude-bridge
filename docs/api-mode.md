# Messages 兼容接口（API 模式）

`/v1/messages` 让支持 Anthropic Messages API 的客户端（桌面应用的第三方 API 模式、编辑器插件、自己的脚本）直接接入 bridge。客户端把 API 地址设为站点根，API 密钥使用 scope 含 `api` 的调用方令牌（`x-api-key: cbc_…` 或 `Authorization: Bearer cbc_…`）。

## 启用

- 服务端：`CLAUDE_BRIDGE_CLIENT_TOOLS=1`（多用户或单口令模式均可）。前置门卫需放行 `/v1/*`，由 bridge 自己校验密钥。
- worker：`--client-tools --content-blocks`，并在 profiles 里提供 `api`：

```json
"api": {"cwd": "~/claude-bridge-work/api/{owner}", "tools": [], "strict_mcp": true,
        "allow_client_tools": true, "emit_content_blocks": true, "client_tool_timeout": 3600}
```

- 建议 `--concurrency 2` 或 `3`：一轮可能长时间等待客户端执行工具，单任务串行时会挡住其他请求。
- 密钥：多用户模式下用户在 `/account` 的「API 接入」自助创建 / 吊销（每人最多 5 个，同时进行的请求上限 3）；管理员在用户详情页查看与吊销。也可用 `claude-bridge callers add <名称> --user <用户名> --scope api`。

## 行为

| 请求 | 处理 |
|---|---|
| `system` | 替换 CLI 自带的系统提示（`--system-prompt-file`） |
| `tools`（带 `input_schema` 的自定义工具） | 注册为调用方工具：模型调用时本次响应以 `stop_reason: "tool_use"` 结束，模型进程挂起等待；客户端执行后在下一个请求里带回 `tool_result`，结果交给同一个进程，后续输出作为该请求的响应。等待期间不计入空闲超时，最长 `client_tool_timeout`；超过 1 小时没有带回结果的轮次会被取消 |
| `tool_use.id` | 模型原生的 `toolu_…` id；名称超过 44 个字符或含特殊字符的工具会以「前缀 + 哈希」注册，返回给客户端时还原 |
| 对话历史 | 每次响应后记录 `指纹(请求消息 + 本次返回内容)`；下一次请求除最后一条外的历史与之相同时，续接同一个 CLI 会话（提示缓存保留）。思考块、空白差异、字符串与块形式的差异不影响指纹。识别不了的历史（被编辑、被压缩）会以文本形式带入一个新会话 |
| `max_tokens` | 每次模型请求的输出上限（`CLAUDE_CODE_MAX_OUTPUT_TOKENS`）。≤ 1 且不带工具的请求视为连通性探测，直接返回，不调用模型；不带工具且 ≤ 1024 的请求（生成标题、摘要等）以一次性对话在 Haiku 上执行 |
| `thinking` | `disabled` 关闭思考，其它值开启 |
| `output_config.effort` | 对应 `--effort` |
| `model` | 含 opus / sonnet / haiku / fable 的名称映射到该系列（已知版本原样使用，未知版本取该系列最新）；响应里的 `model` 原样回显请求值 |
| `temperature`、`top_p`、`stop_sequences`、`tool_choice`（除 `none`） | CLI 不提供对应控制，忽略 |
| 图片（base64） | 存为上传文件后随消息传入；URL 图片以链接文本传入 |

流式响应在排队和等待期间发送 SSE 注释行保持连接，模型开始输出后按官方事件顺序发送（`message_start` … `message_stop`）；使用量取自模型原生事件。

错误使用官方格式 `{"type":"error","error":{"type":…,"message":…}}`：密钥无效 401、账户不可用 403、参数错误 400、同时进行的请求超过上限或额度不足 429（流开始后出现的额度错误以 `error` 事件发送，类型 `rate_limit_error`）、worker 离线 529。

`/v1/messages/count_tokens` 返回估算值（约 3.5 字符一个 token，图片约 1600）。`/v1/models` 列出 worker 上报的模型与已知型号。

## 限制

- 背后是 Claude Code CLI：每轮有数秒的启动开销；采样参数不可控；一个 worker 的并发由 `--concurrency` 决定。
- 用量记在密钥所属账户名下，受该账户的份额上限与整账户保护线约束。
