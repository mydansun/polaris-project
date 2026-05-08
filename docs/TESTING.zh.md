# 测试

## 静态检查

仓库用单一根级 uv workspace,所有命令解析到由
`uv sync --all-packages --all-extras` 物化的共享 `.venv/`。

```sh
uv run ruff check apps/api packages/agent-core apps/worker
uv run --package polaris-api pytest apps/api/tests -v
uv run --package polaris-worker pytest apps/worker/tests -v
uv run --package polaris-design-intent pytest packages/design-intent/tests -v

# scripts/ 自带独立环境(PEP 723 inline deps)。
cd scripts && uv run --group dev pytest

# 前端 type-check 跑在 web 容器里(不需要宿主装 pnpm)。
docker compose -f compose.dev.yaml run --rm web pnpm typecheck
```

## IDE 冒烟测试

`packages/ide/Dockerfile` 在 `./scripts/build.py --only ide` 的过程中
自动跑 Playwright 测试:
- HTTP 200(不是 404)、Theia shell 渲染、定制 welcome 页、
  Explorer 展开、无 trust dialog。

要在宿主交互式跑:
`cd packages/ide && yarn build && yarn start &` → `yarn test`。

## 快速冒烟(端到端)

```sh
./scripts/down.py dev --clear && ./scripts/up.py dev
```

打开 `https://${POLARIS_DOMAIN}/`(默认建议 `polaris-dev.xyz`),
登录(或点 **Dev Login**),发一条 prompt。

## 鉴权流

1. **新用户**:邮箱 → `invite_required` → 邀请码 → 验证码 → 注册完成
2. **老用户**:邮箱 → 验证码 → 登录
3. **限流**:每邮箱每小时第 6 次 → 429
4. **语言**:header 菜单切换 → 即时,localStorage 持久化

## 多 agent 流程

### Discovery-then-build(项目第一条消息)

1. 创建项目 → 空 workspace
2. 第一条消息自动路由到 `mode: "discover_then_build"`
3. Discovery SSE 进度:
   `discovery:clarifying`(1–3 轮 ClarificationCard)→
   `discovery:references` → `discovery:compiled` → `discovery:moodboard`
4. `MoodBoardBody` 在 chat 里出现,带生成的图
5. Codex 接手(同一 session,新 `AgentRun`),走 `plan` mode
6. Plan 出来 → "Proceed" 按钮带 Tabs(Overview / Details)
7. 点 Proceed → 新 session,`build_direct` → Codex 执行

### Build-direct(后续消息)

1. 发第二条以上的消息 → `build_direct` session → Codex 直接写代码,
   不再 plan 一轮。
2. Codex 调 `request_user_input` 时弹 `ClarificationCard`。

plan/proceed 握手只在项目的**第一条**消息(`discover_then_build`
内部)发生 —— 后续消息跳过这一轮,迭代无摩擦。

### Stop / interrupt

1. session 处于 `running` 时点输入栏的 Stop 按钮。
2. Header 状态 pill ~100ms 内翻到 "interrupted"(前端把
   `POST /sessions/{id}/interrupt` 的响应乐观合并)。
3. 对 Codex session:codex app-server 收到 WS 上的 `turn/interrupt`;
   outcome 落到 `status="interrupted"`。
4. 对 Discovery session:`run_design_intent` 的 asyncio 任务在下一个
   await 点被 cancel;outcome 落到 `"interrupted"` ——
   orchestrator 短路掉剩余 agent 链。

### Playwright 冒烟(agent 侧)

1. Agent 调 `focus_browser` → 右栏自动切 VNC
2. Agent 调 `playwright` MCP 工具 → 用户实时看
3. 前端 MCP overlay 给调用做 debounce

## Unsplash + Iconify MCP

```sh
# 从 DB 拿一个 workspace token:
psql -d polaris -c "SELECT workspace_token FROM workspaces LIMIT 1;"

# Unsplash
curl -sSN -X POST "http://localhost:8000/mcp/" \
  -H "Authorization: Bearer <token>" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"search_photos","arguments":{"query":"coffee shop interior","per_page":3}}}'

# Iconify
curl -sSN -X POST "http://localhost:8000/mcp/" \
  -H "Authorization: Bearer <token>" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"search_icons","arguments":{"query":"home","prefix":"lucide"}}}'
```

## Publish

```sh
# 在 workspace 里,项目根目录:
polaris scaffold-publish                        # 打印 stack 菜单 + 检测
polaris scaffold-publish --stack=spa            # (或 node / python / static / custom)
polaris prepublish-audit                        # 静态检查;--deep 加 LLM 审查
git add . && git commit -m "pub"
polaris publish

# 宿主侧:
curl -I https://<uuid>.prod.polaris-dev.xyz/
```

smoke 失败时,平台在销毁预览栈之前抓用户服务的容器日志
(`docker logs --tail 200`)进 `smoke_log` —— 看 SSE 实时输出
里 "captured tail of `<svc>` container logs" 那段(或 DB 里的
`deployments.smoke_log`)。

## Workspace 重启

1. Header 菜单 → Restart workspace → shadcn Dialog 确认
2. IDE + VNC 显示骨架 / loading
3. 就绪后自动重载(~10–20 秒)

## 验证查询

```sql
-- 最近的用户 / 项目
SELECT id, email, name FROM users ORDER BY created_at DESC LIMIT 5;
SELECT id, name, slug FROM projects ORDER BY created_at DESC LIMIT 5;

-- Session + agent_runs + events
SELECT id, sequence, mode, status, LEFT(user_message, 60) FROM sessions ORDER BY created_at DESC LIMIT 5;
SELECT run.id, run.agent, run.status, sess.sequence
  FROM agent_runs run JOIN sessions sess ON run.session_id = sess.id
  ORDER BY run.created_at DESC LIMIT 10;
SELECT kind, status, COUNT(*) FROM events WHERE run_id = '<run-id>' GROUP BY kind, status;

-- Clarification
SELECT request_id, status FROM clarifications ORDER BY created_at DESC LIMIT 5;

-- Design intents(discovery 产出)
SELECT project_id, status, mood_board_url IS NOT NULL AS has_mood_board,
       LEFT(compiled_brief, 80) FROM design_intents
  ORDER BY created_at DESC LIMIT 5;

-- Unsplash 去重缓存
SELECT photo_id, size, s3_key FROM unsplash_images ORDER BY created_at DESC LIMIT 10;

-- Deployments
SELECT id, status, LEFT(git_commit_hash, 7), domain FROM deployments ORDER BY created_at DESC LIMIT 5;
```

## 并发配额

```sh
# 默认值(6 全局 / 每用户 2)下,以同一用户开 3 个浏览器 tab 各发一条
# session;第三个会弹 `QuotaDialog`("用户配额不足 / you're already
# running 2 active sessions")。7 个 tab 跨不同用户 → 其中一个会
# 撞到全局上限。

# 看 sorted sets:
docker exec polaris-redis-1 redis-cli ZRANGEBYSCORE polaris:runs:global -inf +inf WITHSCORES
docker exec polaris-redis-1 redis-cli KEYS 'polaris:runs:user:*'
```
