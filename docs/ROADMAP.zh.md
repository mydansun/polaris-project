# 路线图

## 当前状态

平台端到端跑通:登录 → 创建项目 → 跟 agent 对话 → 第一条消息自动走
**discovery**(LangGraph clarifier + references + compiler + mood-board)
→ Codex 接手,plan、scaffold、写代码、浏览器预览 → 发布到
`<uuid>.prod.polaris-dev.xyz`。

## 已验证的里程碑

### Session / 多 agent
- **Session / AgentRun / Event 模型** —— 每条用户消息一个 Session,
  每个 Session 1+ AgentRun(discovery → codex)。前端 + 后端端到端
  都是 session-native 的。
- **三种 session mode** —— `discover_then_build`(项目第一条消息,
  前端自动路由)、`build_direct`(前端默认,从第二条消息以及"plan
  上的 Proceed 按钮"出发)、`build_planned`(后端在 `mode` 省略时
  的默认值;前端不发)。
- **全链路 interrupt** —— Stop 按钮发 `POST /sessions/{id}/interrupt`;
  API 推一帧终止 SSE(UI 立刻翻状态)+ 给 control 通道发信号;
  worker 的 `_consume_session_control` 转发给当前 agent
  (`CodexAgent.handle_control` → `turn/interrupt` WS;
  `DiscoveryAgent.handle_control` → 任务取消);
  outcome 落到 `_finalize_session(status="interrupted")`。
- **并发配额** —— Redis sorted-set 令牌限 `POST /sessions`
  (`POLARIS_MAX_GLOBAL_RUNS=6`、`POLARIS_MAX_USER_RUNS=2`),
  在路由里同步 acquire,在 worker orchestrator 的 finally 释放。
  HTTP 429 → 前端 `QuotaDialog`。
- **Discovery agent(packages/design-intent)** —— LangGraph 流水线:
  clarifier ⇄ clarifier_ask → review_step → pinterest → compiler →
  mood_board_step → END。每个节点的 SSE 事件由一个 LangChain
  callback handler 推。
- **Pinterest 参考图** —— 拉 6 张候选,batched multimodal LLM scorer
  打分挑 1 张(≥ 阈值或最高分),只有这张喂给 compiler。query
  尾部机械加 "web design"。
- **Mood board 生成器** —— gpt-image-1.5 的 `images.edit`,把
  Pinterest 参考图作为视觉锚 + intent-填充的 prompt。上传 S3 给前
  端卡片渲染;写到 `/home/workspace/mood_board.png` 并在 AGENTS.md
  里引用,让 Codex 可以按需打开。
- **LLM 生成的配色** —— clarifier 调一个专用工具
  `propose_color_palette`;`palette_step` 图节点产出 5 个跟项目语境
  匹配的 hex 选项(正则验)外加遇到解析失败时回退到中性默认。
- **Plan 翻译** —— 每条 Codex plan 都被一次单独的 gpt-5.4 调用重写成
  非技术化的 "Overview";前端用 shadcn Tabs 卡片渲染(Overview / Details)。

### Codex 集成
- **Plan mode** —— Codex 先 plan,turn 结束,前端展示 "Proceed" 按钮。
  点了之后创建一个带本地化触发消息的 `build_direct` session。
- **Codex `request_user_input`** —— 内置的结构化追问工具。worker 在
  Redis pubsub 上 block;前端渲染 `ClarificationCard`;discovery 共
  用同一个通道。
- **动态工具** —— `set_project_root`(IDE 切换 + git init)和
  `focus_browser`(playwright MCP 调用前自动把右栏切到 VNC)。
- **MCP server(给 Codex 用)** —— streamable-HTTP 挂在 `/mcp`,bearer
  workspace token 认证。工具:`search_photos`(Unsplash → S3),
  `get_all_icon_sets` / `get_icon_set` / `search_icons` / `get_icon`
  (Iconify 透传)。secret 留在平台侧。
- **Frontend skill** —— OpenAI 上游的 `frontend-skill/SKILL.md` 烘焙
  进每个 workspace 的 `$HOME/.agents/skills/frontend-skill/SKILL.md`。

### 平台
- **Theia IDE** —— 定制版 `packages/ide`,只留 Explorer + Search + Editor。
  Playwright 冒烟测试在 Docker build 里跑。
- **i18n** —— react-i18next,en + zh。自动探测 + 切换 + localStorage。
- **两栏布局** + 可拖拽的中分线,带 overlay 预览。
- **邮箱验证码 + 邀请码鉴权** —— Postmark 投递,邀请码门禁后自动注册。
- **WebSocket 存活探活** —— `is_alive()` probe,无 idle 超时(900 秒
  wall-clock 上限)。
- **Selkies VNC 加固** —— 音频 / 共享 / 手柄锁定关闭。
- **Chat 特性** —— session 分页、噪声折叠、Ctrl/Cmd+Enter、
  MCP overlay、空消息抑制、plan tabs、mood board 卡片。
- **Publish pipeline** —— `polaris` CLI、smoke test、`secrets.env` 的
  `$` 转义。
  - 菜单驱动的 `polaris scaffold-publish`(无 `--stack` 打印菜单;
    显式 `--stack=<choice>` 才写文件)。五种 stack:`spa`
    (Vite → nginx multi-stage)、`node`、`python`、`static`、`custom`。
  - Compose sanitizer 在构建前从用户的 `compose.prod.yml` 里剥掉
    宿主 `ports:`(避免跟平台 Traefik 抢 80/443)。
  - `prepublish-audit` 有一条对裸 node 二进制(`next` / `vite` /
    `tsc` 不挂在 `npm`/`npx` 下)的静态规则,以及可选的 `--deep`
    LLM 审查,走 `POST /projects/{id}/prepublish-audit`。
  - smoke 失败时,pipeline 在 `compose down -v` 之前抓
    `docker logs --tail 200` 进 `smoke_log`,真正的崩溃原因通过
    SSE 流送到 Codex。
  - Node 模板的 runner 阶段设了 `ENV PATH=/app/node_modules/.bin:$PATH`,
    模板派生的 Dockerfile 里裸 `next` 之类的也能跑通。
- **S3 / MinIO** —— bucket + 匿名可读的 `static/*` 前缀。专用的
  `*.s3.polaris-dev.xyz` 证书。Unsplash MCP + mood board 存储用。
- **Unsplash 去重** —— `unsplash_images` 表按 `(photo_id, size)` 索引
  防重传。对象落在 `static/images/up/<uuid>.ext`(把 Unsplash 上传
  跟手放的平台 asset `static/images/frontend/*` 隔离开)。
- **Welcome 卡片** —— 没选项目时,`ExampleProjectCards` 展示四张
  本地化 prompt 卡片(golf / todo / blog / estate),点哪张就发起一个
  `discover_then_build` session。

## 还没做的

1. `agent_runs.cost_jsonb` / `sessions.cost_jsonb` 上的用量 / token /
   成本会计
2. `project_versions` 的版本 diff UI
3. 多租户鉴权(per-tenant `auth.json`)
4. Worker 重试 / 死信处理
5. Registry GC / 镜像保留策略
6. 远程发布主机
7. Traefik 面板鉴权
8. 用户自定义域名
9. Theia 里的语法高亮(需要 `@theia/plugin-ext`)
10. Design-intent 历史的前端展示(re-discovery UI)
11. `generate_image` MCP 工具(给 Codex 用的装饰 / 抽象图生成,补
    Unsplash 照片和 Iconify 图标之外的能力)
