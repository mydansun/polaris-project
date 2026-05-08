# Polaris 架构

## 系统总览

```
用户浏览器(React 19 + Vite 7)
  ├─ 左:Chat 控制台(SSE 推 session 事件)
  └─ 右:Theia IDE / Chromium VNC(toggle 切换 browser / IDE / 隐藏)

前端 ↕ REST / SSE

FastAPI API(apps/api)
  ├─ 鉴权:邮箱验证码 + 邀请码 + dev-login
  ├─ 项目、workspace、session、版本
  ├─ Clarification 请求/响应(阻塞 Codex 往返)
  ├─ Publish pipeline + dev-dep slot 管理
  ├─ Redis XSTREAM 入队
  └─ Streamable-HTTP MCP server 挂在 /mcp(给 Codex 的 Unsplash + Iconify 工具)

Worker(apps/worker,宿主侧)
  ├─ 从 Redis 消费 session 任务
  ├─ Orchestrator 按 Session 跑 1+ AgentRun
  │     • DiscoveryAgent —— LangGraph(clarifier → review → references → compiler → mood_board)
  │     • CodexAgent     —— 长连 WebSocket 到 workspace 里的 codex app-server
  ├─ 动态工具:set_project_root、focus_browser
  ├─ 把每个节点的 SSE 事件 fan-out;持久化到 sessions / agent_runs / events
  └─ 把 AGENTS.md + mood_board.png 写进 workspace 容器

Codex App Server(workspace 容器内,端口 4455)
  ├─ exec_command、apply_patch、request_user_input(容器即沙箱)
  ├─ MCP 客户端:
  │     • playwright(stdio,跟 chromium-vnc:9223 说话)
  │     • polaris(streamable HTTP,url → apps/api /mcp,bearer = workspace_token)
  ├─ 文件系统 skill:$HOME/.agents/skills/frontend-skill/SKILL.md
  └─ Shell CLI:polaris(发布)、polaris-bg(dev server)

Edge:Traefik v3 @ :80/:443
  ├─ Dev:     polaris-dev.xyz、ide-*.polaris-dev.xyz、browser-*.polaris-dev.xyz
  ├─ S3/MinIO:s3.polaris-dev.xyz / polaris.s3.polaris-dev.xyz(匿名 static/*)
  └─ 发布:    <uuid>.prod.polaris-dev.xyz
```

## Session / AgentRun / Event 数据模型

每条用户消息产生一个 **Session**。orchestrator 在 Session 内部按顺序
跑 1+ **AgentRun**,每个 AgentRun 推一串 **Event**。多 agent 工作流
(discovery → codex)在 DB 里是一等公民。

```
Session(每条用户消息一个)
  ├─ mode: build_planned | build_direct | discover_then_build
  ├─ status: queued | running | completed | interrupted | failed
  └─ AgentRun[] (agent = discovery | codex)
        ├─ input_jsonb、output_jsonb
        ├─ status、started_at、finished_at
        └─ Event[]
              ├─ kind: codex:agent_message | codex:plan | codex:file_change
              │        | codex:command_execution | codex:reasoning
              │        | codex:mcp_tool_call | codex:dynamic_tool_call
              │        | codex:web_search | codex:error | codex:other
              │        | discovery:clarifying | discovery:references
              │        | discovery:compiled | discovery:moodboard
              └─ status (running → completed|failed)、payload_jsonb
```

Session mode:

| Mode | AgentRun 序列 | 触发时机 |
|---|---|---|
| `discover_then_build` | DiscoveryAgent → CodexAgent(`plan`) | 项目第一条消息(前端自动路由) |
| `build_direct`  | Codex `default` | 前端默认,从第二条消息以及 plan 上的 Proceed 按钮 |
| `build_planned` | Codex `plan` | 后端在 `mode` 省略时的默认值;前端不发(留给想每轮 plan 的脚本调用方) |

前端故意跳过迭代轮的 plan/proceed 握手 —— 第一次的
`discover_then_build` 产出 plan 后,用户批准一次,后续消息直接走
`build_direct`,agent 直接编辑代码,不再走一轮 plan。

## 数据库 Schema

PostgreSQL 主存储,Redis 用作队列 + pubsub。

| 表 | 关键字段 |
|---|---|
| `users` | id、email(唯一)、name、avatar_url |
| `verification_codes` | id、email、code、expires_at、used_at |
| `projects` | id、user_id、name、slug、codex_thread_id |
| `workspaces` | id、project_id、repo_path、project_root、workspace_token、ide_status |
| `sessions` | id、project_id、workspace_id、sequence、user_message、mode、status、final_message |
| `agent_runs` | id、session_id、agent、input_jsonb、output_jsonb、status |
| `events` | id、run_id、sequence、external_id、kind、status、payload_jsonb |
| `clarifications` | id、request_id、session_id、run_id、status、questions_jsonb、answers_jsonb |
| `design_intents` | id、project_id、session_id、intent_jsonb、compiled_brief、pinterest_refs_jsonb、pinterest_queries_jsonb、mood_board_url、status |
| `unsplash_images` | id、photo_id、size、s3_key、content_type(给 Unsplash MCP 的去重缓存) |
| `browser_sessions` | id、project_id、workspace_id、status、vnc_url |
| `deployments` | id、project_id、image_tag、domain、status、build_log、smoke_log |
| `workspace_dep_services` | id、workspace_id、service、container_name、status |

## Discovery Agent(packages/design-intent)

LangGraph 流水线,把含混的用户消息变成结构化 design brief + mood
board 参考。由 worker 的 `DiscoveryAgent` 持有,后者把它适配到通用的
`Agent` 接口,在节点切换时推 SSE 事件。

```
     ┌──── clarifier_step ⇄ clarifier_ask(interrupt 等用户回答)──────┐
START→│                          ↓                                        │
     │  review_step(LLM 质量门;reject → 回 clarifier)                   │
     │                          ↓                                        │
     │  pinterest(拉候选 + batched LLM scorer 选 1)                     │
     │                          ↓                                        │
     │  compiler(multimodal gpt-5.4:看选中的图 + intent → brief)        │
     │                          ↓                                        │
     └ mood_board_step(gpt-image-1.5 images.edit + Pinterest ref → PNG)─┘
                                 ↓
                               END
```

graph 返回后 worker:
1. 把 18-key 的 `DesignIntent` + brief 持久化进 `design_intents`。
2. 把 mood board PNG 上传到 S3(`static/images/moodboard/<uuid>.png`),
   并写到 workspace 容器的 `/home/workspace/mood_board.png`。
3. 把 `AGENTS.md`(brief + mood board 绝对路径 + "这是情绪参考,
   不是页面截图")渲染到 `$CODEX_HOME/AGENTS.md`。
4. 把控制权交给 `CodexAgent` 进入 build run。

## Codex 集成

Codex app-server 跑在 workspace 容器内,作为 supervisord 进程。
worker 的 `CodexAgent` 通过 `PolarisCodexSession`(`packages/agent-core`)
建立 WebSocket,按项目维度驱动一个 JSON-RPC 线程。

每轮重建的 binding(每次 run 都基于当前 conn / redis / session 句柄
新建):

| Binding | 用途 |
|---|---|
| `dynamic_tool_handler` | 处理 `set_project_root`、`focus_browser` 工具调用 |
| `user_input_handler`   | 让 Codex 的 `request_user_input` 在 Redis pubsub 答案上 block |

每个 Codex session 内可见的 MCP server:

| Server | 传输 | 部署在 |
|---|---|---|
| `playwright` | stdio(npx 子进程) | workspace 容器内 |
| `polaris`    | streamable HTTP + bearer | **apps/api `/mcp`** —— Unsplash + Iconify 工具 |

`polaris` MCP 的 bearer 是 `services/compose.py` 注入到 workspace 的
per-workspace token(`POLARIS_WORKSPACE_TOKEN`)。Codex 配置
(`infra/workspace/codex-config.toml`)通过 `bearer_token_env_var`
字段读取。

## Codex Agent 工具层

| 类别 | 工具 |
|------|------|
| Codex 原生 | `exec_command`、`apply_patch`、`request_user_input` |
| Shell CLI | `polaris-bg`(dev server)、`polaris publish/scaffold-publish/dev-up` |
| Dynamic(Polaris) | `set_project_root`(IDE 切换 + git init)、`focus_browser`(自动把右栏切 VNC) |
| MCP — playwright | 全套浏览器控制,指向 `http://chromium-vnc:9223` |
| MCP — polaris    | `search_photos`(Unsplash → S3)、`get_all_icon_sets`、`get_icon_set`、`search_icons`、`get_icon` |
| Skill | `$HOME/.agents/skills/frontend-skill/SKILL.md`(设计纪律指南) |

## MCP Server(/mcp)

`apps/api/src/polaris_api/mcp_app.py` 在 `/mcp` 挂一个 FastMCP
streamable-HTTP 端点。鉴权是 bearer token(workspace token),
通过 Starlette ASGI middleware 拦。

| 工具 | 后端 |
|---|---|
| `search_photos(query, per_page, orientation?, color?, content_filter?)` | Unsplash API → 转存 S3 的 `static/images/up/*`,通过 `unsplash_images` 去重 |
| `get_all_icon_sets` / `get_icon_set(set)` / `search_icons(query, limit?, start?, prefix?)` / `get_icon(set, icon)` | api.iconify.design(无 key、无状态透传 + 框架代码片段) |

Secret(UNSPLASH_ACCESS_KEY、S3_*)留在平台侧,workspace 容器只知道
自己的 workspace token。

## 需求澄清

discovery(LangGraph 通过 `interrupt()` 调 `clarifier_ask`)和 Codex
(`request_user_input`)都用这套。两条路径在前端落到同一张卡片。

1. Agent 推结构化问题 → `POST /clarify/request` 持久化 + 推
   `clarification_requested` SSE。
2. Worker 在 `clarification_channel:<run_id>` Redis pubsub 上 block。
3. 前端在 chat 里内联渲染 `ClarificationCard`。
4. 用户提交 → `POST /clarify/response` 在通道 publish。
5. Agent 拿到答案解 block。

问题用非技术化语言。视觉方向选项由 clarifier 的 system prompt 做
行业定制;5 色**主配色每个项目通过专门的图节点 LLM 生成**:

- Clarifier LLM 发一个 `propose_color_palette(industry,
  visual_direction, audience, language)` 工具调用
- `palette_step`(`nodes/clarifier.py`)用一段 color-theorist system
  prompt 跑 `compiler_model`(旗舰),把响应解成
  `[{id, label, swatch}]` × 5,hex 正则验
- 解析 / LLM 失败时回退到中性默认配色,clarifier 循环不会死锁
- 返回的选项直接喂下一次 `ask_questions` 作为色彩问题的 choices ——
  前端的 `ClarificationCard` 在每个 choice 都带 `swatch` hex 时把色
  卡 chip 放大

## IDE(packages/ide)

定制版 Theia,只留 Explorer / Search / Editor,发布为
`polaris/ide:latest`。target:`browser`(Node 后端,无 Electron)。
Playwright 冒烟测试在 Docker build 内跑 —— 测试失败 build 失败。

## Workspace 设计

- **空 workspace 不变量**:启动时为空,scaffolder 先跑
- **IDE**:Theia 在 3000 端口,workspace dir 通过 CLI 参数传入
- **Dev deps**:独立 docker 容器(postgres/redis)挂在 workspace 网络
- **Welcome 页**:nginx sidecar 在 `http://welcome/`
- **容器工具**:Node 24、Python 3 + venv、git、curl、wget、unzip、zip、
  jq、ripgrep、build-essential、Codex CLI、Playwright MCP
- **文件系统 skill**:`/home/workspace/.agents/skills/frontend-skill/SKILL.md`
  (OpenAI 上游 frontend-skill 原文不动)
- **生成的 asset**:`/home/workspace/mood_board.png`(discovery 输出)

## Publish Pipeline

由 chat 里的 Codex 通过 `polaris publish` CLI 触发。

**Scaffold**:`polaris scaffold-publish`(无 `--stack`)打印 5 种 stack
菜单 —— `spa`(Vite / Astro / CRA → nginx multi-stage)、`node`(长跑
Node server)、`python`、`static`、`custom` —— 加上按 marker 检测的
推荐项。CLI 只在 `--stack=<choice>` 显式传入时才写模板文件。
平台侧的 `auto_scaffold_if_missing` 是兜底:用户没跑 CLI 直接点
publish 时也会用同一套检测逻辑(读 `package.json` 依赖里的 `vite` key
区分 `spa` 和 `node`)。

**Audit**:`polaris prepublish-audit` 跑静态规则检查
`polaris.yaml::start`(标出 `next` / `vite` / `tsc` 这种裸框架二进制
—— 运行时 PATH 不带 `node_modules/.bin`,这些会以 exit 127 挂)。
`--deep` flag 额外调平台的 `POST /projects/{id}/prepublish-audit`,
让 LLM 审查语义错配(端口不一致、缺脚本、非幂等迁移)。

**Pipeline**(`apps/api/src/polaris_api/services/publish.py`):

1. **Git archive** → 把 commit 解开到临时目录。
2. **Sanitize** —— `sanitize_prod_compose` 从用户的
   `compose.prod.yml` 里剥掉所有宿主侧 `ports:`(Traefik 占住宿主
   80/443;用户 compose 任何宿主 publish 都会撞)。剥掉的内容追加
   到 `build_log`,用户能看到都改了什么。
3. **Docker build** → `<registry>/polaris/<project>:<short-hash>`。
4. **Secrets** 物化到 `.data/projects/<uuid>/secrets.env`(`$` 转义,
   多次发布之间稳定,DB 卷继续可用)。
5. **Smoke** —— 在隔离网络上拉起 `compose.prod.yml` + 一个
   `compose.preview.yml` override,用一次性的 `curlimages/curl` 容器
   探活发布服务。失败时 finally 块在 `compose down -v` **之前**把
   `docker logs --tail 200 <service>-1` dump 进 `smoke_log` ——
   SSE 流把这段送到 workspace 内 `polaris publish` 的 stdout,
   Codex 看到的就是真实崩溃原因(比如 `sh: 1: next: not found`),
   而不是难以诊断的 curl 错。
6. **Push** 镜像到本地 registry。
7. **Promote** —— 写出 prod override(`compose.polaris.yml`),带上
   Traefik 标签 + `traefik-public` 网络 + 物化的 secret,`compose up`。

回滚复用本地 registry 里缓存的镜像。

**模板**在 `infra/publish-templates/{spa,node,python,static}/`,COPY
进 workspace 镜像的 `/opt/polaris-publish-templates/`,容器内 CLI
直接读。`node` runner 阶段设 `ENV PATH=/app/node_modules/.bin:$PATH`,
模板派生的 Dockerfile 里"裸 `next`"这个常见坑点不会到 prod。

## Session Interrupt

点 Stop 触发一条 5 步流程;5 步都需要,因为 UI 响应性和 worker 真正
合作是两回事:

1. **前端** `App.tsx::handleInterrupt` → `POST /sessions/{id}/interrupt`;
   把返回的 `SessionResponse` 乐观合并进本地 state(立刻翻 `interrupted`)。
2. **API 路由** 在 `session_control_channel(id)` 上 publish
   `{kind: "interrupt"}`,把 DB 里 `sessions.status` 翻成
   `"interrupted"`,并在 `session_events_channel(id)` 上 publish 一帧
   **终止性** `session_completed(status=interrupted)`,任何 SSE 订阅者
   立刻翻状态(worker 后续再走一遍 finalize;重复终止帧幂等 ——
   前端在第一帧终止帧上就关掉 EventSource)。
3. **Worker** `_consume_session_control` 通过
   `agent.handle_control(event)` 把 interrupt 转给当前活跃 agent。
   `CodexAgent.handle_control` 在 Codex WebSocket 上发
   `turn/interrupt`;`DiscoveryAgent.handle_control` cancel 在飞的
   `run_design_intent` asyncio 任务。
4. **Agent 返回路径** —— 都把自己的 interrupted 状态映射成
   `RunOutcome(status="interrupted")`。
5. **Orchestrator** 把 `outcome.status == "interrupted"` 当终止
   (跟 `"failed"` 平行),调 `_finalize_session(status="interrupted")`,
   并在 agent 链每步前轮询 `sessions.status`,这样 API 在 agent 之间
   到达的 interrupt 也能短路。

## 并发配额

两个 Redis sorted-set 令牌限制 session 创建,封顶 OpenAI /
gpt-image-1.5 成本:

| Key | Score | Member |
|---|---|---|
| `polaris:runs:global` | `now + TTL` | `session_id` |
| `polaris:runs:user:<user_id>` | `now + TTL` | `session_id` |

`POST /projects/{id}/sessions` 通过一段 Lua 脚本原子地 acquire 两个
令牌(每次:`ZREMRANGEBYSCORE` 过期 → `ZCARD` 检查上限 → `ZADD`;
user-bucket 拒绝时回滚刚拿的 global slot)。Worker orchestrator 在
`finally` 释放,无论结果如何;TTL 是崩溃恢复兜底。被拒时 API 返回
`HTTP 429 {detail: {reason, limit}}`;前端通过 `QuotaDialog` 表面化。
默认值:6 全局 / 每用户 2 / 1800 秒 TTL(env
`POLARIS_MAX_{GLOBAL,USER}_RUNS` / `POLARIS_RUN_QUOTA_TTL_SECONDS`)。

## 网络

| 网络 | 用途 |
|------|------|
| `polaris-shared`   | 平台基础设施 ↔ per-workspace compose |
| `traefik-public`   | Workspace + chromium + MinIO + 已发布 app(Traefik 标签发现) |
| `<compose>_default` | 每 workspace 隔离 |

## 安全

- 容器即沙箱(`sandbox_mode = "danger-full-access"`)
- Per-workspace 网络隔离,UID 1000(非 root)
- Selkies VNC 加固(音频/共享/手柄禁用)
- `X-Polaris-Workspace-Token` header,容器内 CLI 鉴权 + MCP bearer
- Traefik 唯一入站
- OpenAI / Unsplash / S3 凭据绝不进 workspace 容器

## 超时与存活

Codex WebSocket 每 30 秒一次 `is_alive()` probe。无 idle 超时。每轮
wall-clock 上限 900 秒(env `POLARIS_CODEX_TURN_TIMEOUT_SECONDS`)。
Discovery 的 LangGraph 自带内部轮次上限(3 轮 ask + 2 次 review reject
+ `tool_choice="any"` 的 no-prose 守卫)。
