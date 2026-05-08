# API 参考

FastAPI 监听 `http://localhost:8000`(traefik 在 `https://polaris-dev.xyz/`
的 `/api/` 路径下做反代)。

## 健康

| Method | Path | 说明 |
|--------|------|-----|
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness(database + redis) |

## 鉴权

邮箱验证码 + 可选邀请码。Session:`polaris_session`,HTTP-only JWT cookie。

| Method | Path | 说明 |
|--------|------|-----|
| POST | `/auth/request-code` | 发验证码。Body:`{ email, invite_code? }`。未注册且无邀请码 → `{ ok: false, reason: "invite_required" }`。限流:每邮箱每小时 5 次。 |
| POST | `/auth/verify-code` | 校验 + 自动注册。Body:`{ email, code }`。Set cookie。 |
| GET  | `/auth/me` | 当前用户 |
| GET  | `/auth/dev-login` | 自动以 dev user 登录(仅本地 dev) |
| POST | `/auth/logout` | 清 session cookie |

## 项目

| Method | Path | 说明 |
|--------|------|-----|
| POST | `/projects` | 创建项目(自动开 workspace) |
| GET  | `/projects` | 列出当前用户的项目 |
| GET  | `/projects/{id}` | 项目详情(含 workspace) |

## Sessions

每条用户消息创建一个 Session。orchestrator 在每个 Session 内部跑
1 个或多个 `AgentRun`(discovery、codex、或两者顺序串)。数据模型见
`docs/ARCHITECTURE.zh.md#sessionagentrunevent-数据模型`。

| Method | Path | 说明 |
|--------|------|-----|
| POST | `/projects/{id}/sessions` | 创建 session。Body:`{ message, mode? }`,mode 为 `discover_then_build`(前端在项目第一条消息发)\| `build_direct`(前端默认,从第二条消息开始,以及 plan 的 Proceed 按钮)\| `build_planned`(后端在 `mode` 省略时的默认值;前端不发 —— 留给想每轮都 plan 的脚本调用方)。撞到并发上限时返回 **HTTP 429**,body 是 `{detail: {reason: "global_quota" \| "user_quota", limit: N}}`。 |
| GET  | `/projects/{id}/sessions?limit=N&before_sequence=M` | 列 session(分页,新→旧) |
| GET  | `/sessions/{id}` | session 详情(agent_runs + 各自的 events) |
| GET  | `/sessions/{id}/events` | SSE 流 |
| POST | `/sessions/{id}/interrupt` | 把 session 状态翻成 `interrupted`,在 control 通道 publish `interrupt`,并推一帧终止性 `session_completed(status=interrupted)` SSE 让 UI 立刻翻状态(worker 随后再走一遍 finalize;重复的终止帧是幂等的)。 |
| POST | `/sessions/{id}/steer` | 在 session 进行中追加用户文本 |

### SSE 事件

所有信封带 `session_id`,相关时还带 `run_id`。

```jsonc
{ "kind": "session_started",   "session_id": "..." }

// agent_runs 生命周期
{ "kind": "run_started",   "run_id": "...", "agent": "discovery" | "codex" }
{ "kind": "run_completed", "run_id": "...", "status": "completed" | "failed" }

// event 行生命周期(每条 codex item / discovery 节点对应一行)
{ "kind": "event_started",   "event_kind": "codex:plan", "sequence": N, "external_id": "...", "payload": {...} }
{ "kind": "event_completed", "event_kind": "codex:plan", "external_id": "...", "payload": {...}, "status": "completed" }

// streaming token deltas(codex:agent_message;不持久化)
{ "kind": "agent_message_delta", "text": "..." }

// 平台信号
{ "kind": "project_root_changed",    "path": "/workspace/..." }
{ "kind": "browser_focus_requested", "reason": "..." }

// status-bar 计数器 —— worker 把 fs / playwright-call 的突发 ~500ms
// 合并成一帧;前端再 throttle 到 ~400ms 触发一次 "+N" 浮动动画
// (见 StatusBar.tsx)。
{ "kind": "session_stats_updated",
  "file_change_count": N, "playwright_call_count": M,
  "file_change_delta": n, "playwright_call_delta": m }

// clarification 往返(discovery + codex 共用同一条路径)
{ "kind": "clarification_requested", "request": { "request_id": "...", "questions": [...] } }
{ "kind": "clarification_answered",  "request_id": "..." }

{ "kind": "session_completed", "status": "completed"|"failed"|"interrupted", "final_message": "..." }
```

### `event_kind` 取值

Discovery 事件由 LangGraph callback handler 推送;Codex 事件镜像
Codex 的 `item.type` 流。

| 组 | 类型 |
|---|---|
| Codex | `codex:agent_message`、`codex:plan`、`codex:reasoning`、`codex:command_execution`、`codex:file_change`、`codex:mcp_tool_call`、`codex:dynamic_tool_call`、`codex:web_search`、`codex:error`、`codex:other` |
| Discovery | `discovery:clarifying`、`discovery:references`、`discovery:compiled`、`discovery:moodboard` |

`discovery:moodboard` 事件的 completion payload 带
`mood_board_url`(生成的 PNG 在 S3 上的 URL),前端据此在 chat 里
渲染一张 mood-board 卡片。

## Clarification

discovery 和 Codex 的结构化追问弹窗都走这套接口。

| Method | Path | 说明 |
|--------|------|-----|
| POST | `/projects/{id}/clarify/request`  | worker 推问题(持久化 + SSE) |
| GET  | `/projects/{id}/clarify/pending`  | 前端 reload 时检查是否有未答的 clarification |
| GET  | `/projects/{id}/clarify/response?request_id=X` | worker 兜底:漏掉 pubsub 时轮询答案 |
| POST | `/projects/{id}/clarify/response` | 前端提交答案 → Redis 在对应的 `(session_id, run_id)` 通道 publish |

## Workspace

| Method | Path | 说明 |
|--------|------|-----|
| GET / POST / DELETE | `/projects/{id}/workspace/runtime` | 管理 workspace compose(GET 仅当 `project_root IS NOT NULL` 时才返回 `ide_url` / `browser_url`) |
| POST   | `/projects/{id}/workspace/runtime/restart` | 重启容器 |
| GET    | `/projects/{id}/workspace/ide` | 当前 Theia IDE session(以 `project_root` 为门禁) |
| POST / DELETE | `/projects/{id}/workspace/ide/session` | 起 / 停 IDE workspace session |
| GET    | `/projects/{id}/workspace/files` | 列文件 |
| GET    | `/projects/{id}/workspace/files/content?path=X` | 读单个文件 |
| PUT    | `/projects/{id}/workspace/files/content` | 写单个文件 |
| POST   | `/projects/{id}/workspace/snapshot` | Git 快照 → `project_versions` |
| GET    | `/projects/{id}/workspace/versions` | 版本历史 |

## Browser Session

| Method | Path | 说明 |
|--------|------|-----|
| GET / POST / DELETE | `/projects/{id}/browser/session` | 浏览器(chromium-vnc)session 生命周期。`workspace.project_root IS NULL` 时 GET 返回 204(静默轮询)。 |

## Dev 依赖

| Method | Path | 说明 |
|--------|------|-----|
| GET    | `/projects/{id}/workspace/dev-deps` | 列已启用的 slot |
| POST   | `/projects/{id}/workspace/dev-deps` | 起 + 加。Body:`{ service: "postgres" \| "redis" }` |
| DELETE | `/projects/{id}/workspace/dev-deps/{service}` | 停 + 删 |

## Publish / Deployments

| Method | Path | 说明 |
|--------|------|-----|
| POST | `/projects/{id}/publish` | 触发发布(202) |
| GET  | `/projects/{id}/deployments?limit=N` | 历史 |
| GET  | `/deployments/{id}` | 详情(带日志) |
| GET  | `/deployments/{id}/events` | SSE 流(`log` / `status` / `ready` / `failed` 帧) |
| POST | `/projects/{id}/rollback` | 回滚。Body:`{ git_commit_hash }` |
| POST | `/projects/{id}/prepublish-audit` | 可选 LLM 审查,被 `polaris prepublish-audit --deep` 调用。Body:`{ polaris_yaml, dockerfile, package_json_scripts }`。返回 `{ issues: [{severity: "error"\|"warning", hint, fix}] }`。静态检查(裸 node 二进制、YAML 形状)在 workspace 侧的 CLI 里;这条端点加的是语义层审查(端口不匹配、缺脚本、非幂等迁移)。`OPENAI_SECRET` 未设时返回 `{issues: []}` —— audit 是 best-effort,不阻断发布。 |

## MCP Server(给 Codex 用)

| Method | Path | 说明 |
|--------|------|-----|
| POST | `/mcp/` | Streamable-HTTP MCP 协议端点。需要 `Authorization: Bearer <workspace_token>`。工具:`search_photos`、`get_all_icon_sets`、`get_icon_set`、`search_icons`、`get_icon`。 |

Codex 配置(`infra/workspace/codex-config.toml`)指向这个 URL,通过它的
`bearer_token_env_var` 字段读取 workspace token。前端不调用这个端点。

## 内部:Unsplash REST 代理

`POST /workspace/unsplash/search` 是一条内部 REST 路由(session cookie
或 workspace-token),用于调试 / smoke 测试。生产路径是 MCP 的
`search_photos` 工具。
