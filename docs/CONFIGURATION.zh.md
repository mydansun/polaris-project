# 配置

## 单一来源

仓库根的 `.env.dev`(给 dev 栈)和 `.env.stage`(给 stage 栈)是配置
来源。`apps/api`、`apps/worker` 和 `packages/design-intent` 这套 LangGraph
读取的是 compose 通过 `env_file:` 指令注入的那个文件。`.env.example`
是共用模板。

```sh
cp .env.example .env.dev    # 自己填,或交给向导
./scripts/up.py dev         # 必要时配置 + 启动 dev 栈
# stage 同理:
cp .env.example .env.stage
./scripts/up.py stage
```

dev 和 stage 互相独立 —— 各自有自己的 env file,各自的 compose project
名(`polaris` vs `polaris-stage`),各自的命名卷。两套栈在同一台主机
上可以共存,不会互相覆盖状态。

## 环境变量

### 数据存储

```
POLARIS_DATABASE_URL=postgresql+asyncpg://root:123456@127.0.0.1:5432/polaris
POLARIS_REDIS_URL=redis://127.0.0.1:6379/0
```

### 鉴权

```
SESSION_SECRET=<随机 32+ 字节 secret>
# Dev Login 快捷方式(GET /auth/dev-login + 登录页的 "Dev Login" 按钮)。
# 留空时端点 404,前端按钮也隐藏(通过 `GET /auth/config`)。
# **只在本地 dev 上设。**
POLARIS_DEV_USER_EMAIL=dev@polaris.local
POLARIS_DEV_USER_NAME=Polaris Dev
POLARIS_INVITE_CODE=               # 新用户注册必需(留空 = 关停所有注册)
```

### 邮件(Postmark)

```
POSTMARK_SERVER_TOKEN=             # Postmark API token
POSTMARK_MESSAGE_STREAM=outbound   # Postmark message stream ID
POSTMARK_FROM_EMAIL=noreply@polaris.dev  # 已验证的发件地址
```

`POSTMARK_SERVER_TOKEN` 留空时验证码打到 API 控制台(本地开发方便)。

### 前端

```
FRONTEND_URL=https://polaris-dev.xyz
POLARIS_CORS_ORIGINS=["https://polaris-dev.xyz"]
VITE_API_BASE_URL=/api
```

### Workspace 镜像

```
POLARIS_WORKSPACE_IMAGE=polaris/workspace:latest   # Theia IDE + dev 工具链 + Codex
POLARIS_BROWSER_IMAGE=polaris/chromium-vnc:latest
POLARIS_POSTGRES_IMAGE=postgres:16-alpine
POLARIS_REDIS_IMAGE=redis:7-alpine
```

### URL 模板

```
POLARIS_IDE_PUBLIC_URL_TEMPLATE=https://ide-{workspaceHash}.polaris-dev.xyz
POLARIS_BROWSER_PUBLIC_URL_TEMPLATE=https://browser-{workspaceHash}.polaris-dev.xyz
```

### Worker(Codex)

```
POLARIS_CODEX_MODEL=gpt-5.4                     # 主 Codex 模型
# POLARIS_CODEX_TURN_TIMEOUT_SECONDS=900        # 每轮 wall-clock 上限
# POLARIS_CODEX_LIVENESS_CHECK_INTERVAL_SECONDS=30
# POLARIS_IDLE_WORKSPACE_TIMEOUT_SECONDS=3600   # 清扫器停掉 idle workspace
# POLARIS_CODEX_PLAN_PLAIN_MODEL=gpt-5.4        # 把 Codex plan 翻成非技术化 "Overview"
```

### OpenAI

```
OPENAI_SECRET=                     # 仅平台侧,绝不进 workspace 容器
```

被以下使用:LangGraph discovery agent(clarifier / review / compiler /
mood_board)、Codex plan-plain 翻译器、MCP `search_photos` 工具的
下游图片调用。

### S3 / MinIO(图片转存)

`static/*` key 前缀匿名可读;构造的 URL 是 `${S3_URL_BASE}/${key}`。
凭据保留在平台侧,绝不注入 workspace 容器。

```
S3_ACCESS_KEY_ID=polaris
S3_SECRET_ACCESS_KEY=<随机 32+ 字节 secret>
S3_ENDPOINT=https://s3.polaris-dev.xyz
S3_BUCKET=polaris
S3_URL_BASE=https://polaris.s3.polaris-dev.xyz

# MinIO root 凭据(只给基础设施容器用,apps/api 不读)
MINIO_ROOT_USER=root
MINIO_ROOT_PASSWORD=<随机 32+ 字节 secret>
```

### Unsplash MCP

```
UNSPLASH_ACCESS_KEY=               # 仅服务端;workspace MCP 走 /mcp 代理
```

MCP 的 `search_photos` 工具下载选中的 Unsplash 图片,转存到上面 S3
bucket 的 `static/images/up/*.jpg`,通过 `unsplash_images` 表按
`(photo_id, size)` 去重。Unsplash 上传放在 `up/` 子前缀下,跟
`static/images/frontend/*` 这种手放的平台 asset 隔离开。

### Design-Intent LangGraph(packages/design-intent)

全部可选,默认值能跑常规情况。Discovery agent 除了 review 步用 mini
模型,其它都用旗舰模型。

```
POLARIS_DESIGN_INTENT_MODEL=gpt-5.4                   # 没单独 override 的角色的兜底
POLARIS_DESIGN_INTENT_COMPILER_MODEL=gpt-5.4          # multimodal brief 写手
POLARIS_DESIGN_INTENT_CLARIFIER_MODEL=gpt-5.4         # tool-calling clarifier
POLARIS_DESIGN_INTENT_REVIEW_MODEL=gpt-5.4-mini       # 廉价 JSON 评分
POLARIS_DESIGN_INTENT_SCORER_MODEL=gpt-5.4-mini       # batched 图片匹配 scorer
POLARIS_DESIGN_INTENT_MOOD_BOARD_IMAGE_MODEL=gpt-image-1.5
POLARIS_DESIGN_INTENT_MOOD_BOARD_SIZE=1536x1024

POLARIS_PINTEREST_TOOL_BASE=http://polaris-dev.xyz:9801
POLARIS_DESIGN_INTENT_MAX_ROUNDS=3
POLARIS_DESIGN_INTENT_PINTEREST_HOPS=1
POLARIS_DESIGN_INTENT_MAX_REFS=6                      # 喂给 batched scorer 的候选数
POLARIS_DESIGN_INTENT_IMAGE_SCORE_THRESHOLD=4.0       # 0–5;首张 ≥ 阈值即取,否则取最高分
```

### 发布

```
POLARIS_PUBLISH_PROJECTS_ROOT=.data/projects
POLARIS_REGISTRY_URL=127.0.0.1:5000
POLARIS_API_URL_FOR_WORKSPACE=http://host.docker.internal:8000
# POLARIS_PUBLISH_BUILD_TIMEOUT=900     # docker build wall-clock 上限(秒)
# POLARIS_PUBLISH_SMOKE_TIMEOUT=60      # smoke-probe 窗口(秒)
```

### 并发配额

两个 Redis sorted-set 令牌限 `POST /projects/{id}/sessions`,封顶
OpenAI / gpt-image-1.5 的成本。在路由里同步 acquire,在 worker
orchestrator 的 `finally` 里释放(进程崩溃时也会通过 TTL 兜底过期)。
见 `apps/api/src/polaris_api/services/run_quota.py`。

```
POLARIS_MAX_GLOBAL_RUNS=6              # 平台范围在飞 session 数
POLARIS_MAX_USER_RUNS=2                # 单用户在飞 session 数
POLARIS_RUN_QUOTA_TTL_SECONDS=1800     # 兜底 TTL(sorted-set score = now + TTL)
```

撞上限的客户端收到 HTTP 429,带 `{detail: {reason: "global_quota" |
"user_quota", limit: N}}`;前端通过 `QuotaDialog` 表面化。

### Prepublish audit(LLM `--deep`)

workspace CLI 的 `polaris prepublish-audit --deep` 把用户的
`polaris.yaml` + `Dockerfile` + `package.json::scripts` 上传到
`POST /projects/{id}/prepublish-audit`,跑一遍 LLM 审查,识别可能的
运行时失败(裸框架二进制、端口不匹配、缺脚本、非幂等迁移)。

```
POLARIS_AUDIT_MODEL=gpt-5.4-mini       # 默认廉价;audit 是文本进文本出
```

需要 `OPENAI_SECRET`;key 缺失或调用失败时返回空 issue —— audit
是 best-effort,自己出基础设施故障也绝不阻断发布。

### Traefik / 域名

```
POLARIS_DOMAIN=polaris-dev.xyz
POLARIS_PROD_DOMAIN_BASE=prod.polaris-dev.xyz
POLARIS_TRAEFIK_PUBLIC_NETWORK=traefik-public
```

## TLS 证书

Traefik 通过 Cloudflare DNS-01 ACME 签三对 Let's Encrypt 通配证书
(平台 + 发布 + S3 三个 plane)。通配 SAN 只匹配一级 label,所以每个
plane 一对。

```
CF_API_TOKEN=<Cloudflare DNS-edit token,作用域到父 zone>
ACME_EMAIL=admin@<你的域名>
```

`./scripts/up.py` 在拉起栈之前会在线校验 token;Traefik 之后自动
签发并续期。**宿主机不跑 `certbot`,不挂 `/etc/letsencrypt/`。**

DNS:`${POLARIS_DOMAIN}`、`*.${POLARIS_DOMAIN}`、
`${POLARIS_PROD_DOMAIN_BASE}`、`*.${POLARIS_PROD_DOMAIN_BASE}`、
`*.s3.${POLARIS_DOMAIN}` 必须全部解析到跑 Traefik 的主机。

## Codex 鉴权

Codex app-server 跑在 workspace 容器里。通过 read-write bind-mount
复用宿主用户的 `~/.codex/auth.json`。在宿主上跑一次 `codex login`。

Session 存在 per-workspace 命名卷里(`polaris-ws-<hash>-codex-home`),
所以容器重启之间 `thread/resume` 仍有效。卷在 workspace 镜像里也预
种了:
- `$CODEX_HOME/config.toml` —— 启用 Playwright stdio MCP + Polaris HTTP MCP
- `$HOME/.agents/skills/frontend-skill/SKILL.md` —— 设计纪律 skill

## IDE(packages/ide)

定制版 Theia 服务在 3000 端口。`packages/ide/Dockerfile` 三段:

1. **builder** —— `yarn install` + `yarn build`(tsc → theia generate
   → inject custom modules → theia copy → webpack)
2. **runtime** —— 精简版 `node:22-bookworm-slim`,带 git + 原生库
3. **test** —— 装 Playwright,起 Theia,跑冒烟测试(HTTP 200、
   Explorer 展开、定制 welcome)。测试失败 build 失败。

最终镜像是干净的 runtime(test deps 丢弃)。

## Chromium VNC(Selkies)

chromium-vnc 容器跑加固过的 Selkies 配置:
- `HARDEN_DESKTOP=true`、`HARDEN_OPENBOX=true`
- 音频、麦克风、手柄、文件传输、sharing、第二屏:全部禁用并锁定
- 所有侧栏面板隐藏并锁定
- Browser cursors 启用(`SELKIES_USE_BROWSER_CURSORS=true|locked`)
- Chrome 主页 + 新标签页:`http://welcome/`

## Welcome 页

```sh
pnpm --filter @polaris/welcome-page build
```

产出 `packages/welcome-page/dist/`。Workspace 启动时,API 把它复制到
该 workspace 的 browser-config 目录;一个 `welcome` nginx sidecar 在
`http://welcome/` 提供服务。`dist/` 缺失时 chromium 进 `about:blank`,
API 打 build 提示日志而非直接失败。

## Secrets.env 转义

所有写入 `secrets.env` 的值,`$` 都被转义成 `$$`,防止 Docker
Compose 把值当变量插值。
