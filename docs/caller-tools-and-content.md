# 完整内容流、逐轮控制与调用方工具

现有网页聊天、图片、Code 工作区和 CLI 会话仍使用原来的接口。本文的扩展面向嵌入 bridge 的应用；调用方工具默认关闭，需要服务端和 worker 同时启用。

## 完整内容块

worker 使用 `--content-blocks`，或设置 `CLAUDE_BRIDGE_CONTENT_BLOCKS=1`（嵌入时 `WorkerConfig(emit_content_blocks=True)`）。保留原有 `delta / thinking / tool_use / tool_result` 展示事件，并额外产生：

- `content_stream`：`data.event` 原样保存 CLI 的 `stream_event.event`，包括文本、`thinking_delta`、`signature_delta`、工具参数 `input_json_delta`、使用量和结束原因。`data.native_message_id` 是模型消息标识，区别于事件外层的 bridge 消息 ID；`at` 是网页展示用的累计正文位置。
- `content_message`：`data.message` 是 CLI 输出的完整 assistant 消息，包括最终内容块、完整思考文本及 opaque signature/redacted data。CLI 不输出 partial events 时也可使用这个事件。

原有 thinking 预览继续遵守 `thinking_max_chars`；完整内容事件不使用这个预览截断。组件在收到思考增量时即更新预览，并合并最后的预览事件，不显示 opaque 签名。未开启的 worker 行为不变。

**只保留 CLI 真正提供的数据**：签名未提供时保持缺失，不生成替代签名。模型可能提供摘要或不公开思考内容。消费完整内容流时不要同时把网页 `delta` 当成原始模型文本拼接：网页轨道会插入段落分隔，重复消费也会把内容计算两遍。

## 逐轮选项

`POST /send` 和 `POST /threads/{id}/messages` 可以携带 `options`，由服务端校验并冻结在本轮 job 中，不修改 `/settings`：

```json
{
  "text": "解释这段代码",
  "options": {
    "model": "sonnet",
    "effort": "high",
    "max_turns": 4,
    "auto_context": false,
    "max_output_tokens": 4096,
    "thinking": true,
    "instructions": "回答简洁，列出实际依据。"
  }
}
```

model/effort/max_turns 使用对应 CLI 控制；instructions 追加到原生 CLI 系统提示。max_output_tokens 使用 `CLAUDE_CODE_MAX_OUTPUT_TOKENS`，thinking 使用 `CLAUDE_CODE_DISABLE_THINKING`。这两个变量取决于安装的 CLI/模型支持，输出上限约束的是**每次模型请求**，不是多工具任务全部轮次的总预算。CLI 原有系统规则、宿主工具权限和工作目录不被替换。未列出的选项返回校验错误，而不是悄悄忽略。

CLI 控制的依据：[环境变量](https://code.claude.com/docs/en/env-vars)、[CLI 参考](https://code.claude.com/docs/en/cli-reference)。这个扩展不声称提供 CLI 没有暴露的采样参数、停止序列或精确思考预算。

## 调用方工具：执行地点明确的原生 MCP 工具

启动配置：

```sh
claude-bridge serve --client-tools
claude-bridge worker --client-tools --content-blocks
```

也可通过 `CLAUDE_BRIDGE_CLIENT_TOOLS=1`，或嵌入时分别设置 `BridgeConfig(client_tools_enabled=True)` 和 `WorkerConfig(allow_client_tools=True)`。worker profile 可以单独设置 `allow_client_tools`；这让宿主选择只在某种 scope 开放。默认不开放。

消息可以声明工具及实际执行环境：

```json
{
  "text": "请用 Echo 返回这个值",
  "client_tools": [{
    "name": "Echo",
    "description": "在请求应用内执行，返回输入文字。",
    "input_schema": {
      "type": "object",
      "properties": {"value": {"type": "string"}},
      "required": ["value"],
      "additionalProperties": false
    }
  }],
  "client_environment": {
    "platform": "win32",
    "working_directory": "F:/project",
    "home_directory": "C:/Users/example",
    "desktop_directory": "C:/Users/example/Desktop"
  }
}
```

worker 加载受控的 `bridge_client` stdio MCP server，模型看到真正注册的 `mcp__bridge_client__Echo` 工具。它不会解析模型的自由文本来猜调用，也不会在 worker 机器上执行输入。MCP server 只发布 `client_tool_call`、等待调用方回传结果，再把结果交回同一个 CLI 进程。其它原有工具继续在 worker 上执行，`client_environment` 不改变 worker 的 cwd。

worker 强制只加载受控 MCP 配置，但保留宿主的内建工具和原有权限列表。应用必须自行决定是否执行调用、请求用户权限并限制可访问文件/命令。声明某个函数不意味着 bridge 获得了调用方机器的权限。

### 结果与重连

- SSE `event` 中的 `client_tool_call` 提供 call_id、name、input 和 deadline。外层 message_id 指向本次 bridge assistant 消息。
- `GET /threads/{id}/client-tools` 返回仍有效的待处理调用。连接晚于调用、SSE 重连或降级轮询时，从这里恢复；不要只依赖实时通知。
- `POST /messages/{message_id}/client-tools/{call_id}/result` 回传 `{"content":"执行结果","is_error":false}`。content 也可为 MCP text/image 块数组；图片仅支持 base64 PNG/JPEG/GIF/WebP，总结果最大 8 MB。
- JS 客户端提供 `pendingClientTools(threadId)` 和 `respondClientTool(messageId,callId,content,{isError})`；`send` 的参数为 `clientTools / clientEnvironment / options`。

调用方应以 call_id 去重并缓存执行结果，网络重试时重发同一结果，不能重新执行副作用。服务端对同一调用 ID 的不同参数、不同结果返回 409；相同结果重发安全。重连的应用也应持久化自己的执行记录，bridge 无法保证跨调用方崩溃的全局 exactly-once。

等待结果默认 300 秒，worker 的 `--client-tool-timeout` 可以调整（最大 900 秒），整个任务仍受 chat_timeout 限制。取消、超时、线程删除会阻止继续提交结果；它们不能代替调用方对已经开始的本机操作实施取消。

### 权限与凭证

结果回传沿用 browser_auth，校验线程归属。worker 使用原有 agent token 创建当前 job 的受限会话；MCP 子进程只收到 job-scoped token，不能访问 agent 控制面或其它 job。重开会话会轮换 token，并让旧的待处理调用失效。只在数据库保存 token 摘要。

工具声明不能指定启动命令、环境变量或 cwd。受控 server 以隔离 Python 模式启动，避免工作区文件遮蔽模块；参数按声明的 JSON Schema 校验，schema 的远程引用不下载。配置文件仅包含受限 token，运行结束后清理；CLI 子进程不继承 bridge 控制面的 password/secret/agent token。

schema 检查与参数验证在独立、可终止的进程运行，单次最长 2 秒，同一服务进程最多并发 4 次。输入限制为 6 MB、50,000 个节点、48 层嵌套；POSIX 上另设 2 秒 CPU 和 384 MB 地址空间上限，Windows 使用进程超时及输入/并发限制。复杂正则、递归引用、验证超时或容量不足会拒绝本次请求，调用方不能依赖任意复杂 schema 均能通过。子进程不继承服务凭证和代理变量。

只有 `POST /api/agent/jobs/{job_id}/client-tool-session` 使用原有全局 agent token。`/api/agent/client-tools/{job_id}/definition`、`/calls` 和 `/calls/{call_id}` 使用受限 token；沿用现有 agent 前缀，因此门户部署不需要给浏览器接口新增免登录路径。
