# 开发

本地开发 Polaris 平台代码时如何搭起来。共享测试 / demo 环境参见
[STAGING.zh.md](./STAGING.zh.md)。

---

## 1. 主机依赖

| 工具 | 用途 | 最低版本 |
|---|---|---|
| Linux 或 macOS | 开发主机 | Ubuntu 22.04+ / macOS 13+ |
| Docker Engine / Desktop | 一切跑在容器里(含 api / worker / web) | 24.x+,compose v2 |
| `uv` | 解析 `scripts/*.py` 的 PEP 723 inline-deps,以及仓库根的 uv workspace | 0.11+ |
| Codex CLI + `codex login` | Workspace 容器 bind-mount 宿主机 `~/.codex/auth.json` | 最新 |
| 一个绑在 Cloudflare DNS 的真实域名 | TLS 走 ACME DNS-01,Traefik 自动签 | — |

不需要系统 Python venv,不需要系统 pnpm,**不需要 Make**。
`./scripts/up.py dev` 自己会预检 Docker 和 `.env.dev`,首次发现必填项
缺失时直接拉起向导。

**`compose.dev.yaml` 占用的宿主端口:**

| 端口 | 绑定到 | 用途 |
|---|---|---|
| 80 / 443 / 8090 | `0.0.0.0` | Traefik(HTTP / HTTPS / dashboard)。8090 无认证 —— 共享主机上请只对 loopback 开放。 |
| 5000 | `127.0.0.1` | 本地 Docker registry |
| 5432 | `127.0.0.1`(动态宿主端口) | Postgres,方便宿主跑 `psql`。`docker compose -f compose.dev.yaml port postgres 5432` 查实际端口。 |

api / worker / web / dev-vnc / minio **不绑**宿主端口 —— 它们挂在
`polaris-shared` 和 `traefik-public` 网络上,通过 Traefik 在
`${POLARIS_DOMAIN}` 上访问(比如 `https://polaris-dev.xyz/`,
`https://vnc.polaris-dev.xyz/`)。

---

## 2. 初次搭建

```bash
git clone <repo> && cd polaris-2
cp .env.example .env.dev   # 模板 —— `up.py` 会带你补完
./scripts/up.py dev        # 首次启动会触发交互式向导
```

(省略 `dev` 位置参数会触发交互式选择。stage 模式同样的形状:
`cp .env.example .env.stage` + `./scripts/up.py stage`。)

向导会逐字段提示输入,在线校验 token(Cloudflare DNS-01、OpenAI、
Pinterest),`SESSION_SECRET` 缺失时自动生成,然后跑
`docker compose -f compose.dev.yaml up -d --build`。字段元数据集中在
`scripts/lib/spec.py`,这是向导、README、CI 非交互模式的唯一事实源。

本地开发的最小 `.env.dev`:

```
POLARIS_DOMAIN=polaris-dev.xyz             # 任意属于你的、托在 CF DNS 上的域名
SESSION_SECRET=<openssl rand -hex 48>      # 留空则向导自动生成
POLARIS_INVITE_CODE=dev-invite             # 任意字符串,新用户注册凭这个
POLARIS_DEV_USER_EMAIL=dev@polaris.local   # 启用 "Dev Login" 按钮 + /auth/dev-login(留空则两者都关)
POLARIS_DEV_USER_NAME=Polaris Dev          # 自动创建的 dev 用户的显示名
OPENAI_SECRET=                             # 本地开发可留空(跳过 discovery agent)
POSTMARK_SERVER_TOKEN=                     # 留空时验证码打到 api 容器 stdout
```

Postmark + OpenAI 都留空时:

- **Dev Login** 可登录,跳过邮件验证。
- 走 discovery agent 链路的消息(每个项目的第一条 / 重新 discover)
  会在 compiler 步失败。本地工作时**从第二条消息开始**就好 ——
  后续消息走纯 Codex 分支,不依赖 OpenAI。
- 其它功能(workspace compose、Theia IDE、chromium VNC、Codex
  session、publish pipeline)都不依赖这两个 key。

### 构建 workspace 运行时镜像

`./scripts/up.py dev` 拉起平台栈本身,但每会话 workspace 容器**用的
运行时镜像**要由 `./scripts/build.py` 来打:

```bash
./scripts/build.py                # 构建 polaris/{ide,workspace,chromium-vnc}:latest
./scripts/build.py --only workspace
./scripts/build.py --force        # 忽略 mtime 缓存,全部重建
```

每个镜像带一个 `polaris.built-at` LABEL,值是 Dockerfile + 关键 context
文件的 mtime。重跑时 context 没变的镜像直接跳过。`polaris/ide` 首次
构建 5–10 分钟(yarn workspaces + `theia generate` + webpack);后续秒级。

### 数据库迁移

`compose.dev.yaml` 里有一个一次性的 `migrate` 服务,在 api/worker
启动前跑 `alembic upgrade head`,所以 `./scripts/up.py dev` 会自动
把 schema 推到最新。如果想在不重启栈的情况下手动跑一次(比如刚改完
迁移文件):

```bash
docker compose -f compose.dev.yaml run --rm migrate
```

(小贴士:在 shell rc 里 `export COMPOSE_FILE=compose.dev.yaml`,
后面所有 compose 命令都能省掉 `-f`。)

---

## 3. 日常命令

所有 `up.py` / `down.py` 命令都接收一个可选的 `dev | stage` 位置参数,
省略时会交互选择(`--non-interactive` 下默认 `dev`)。本页假设 `dev`,
stage 模式见 [STAGING.zh.md](./STAGING.zh.md)。

| 命令 | 用途 |
|---|---|
| `./scripts/up.py dev` | 配置(必要时)+ 启动平台栈。改了 `.env.dev` 之后再跑一次。 |
| `./scripts/up.py dev --reconfigure` | `.env.dev` 完整也强制走向导。 |
| `./scripts/up.py dev --non-interactive` | CI 模式 —— 任何必填项缺失立刻报错。 |
| `./scripts/up.py dev --skip-build` | Dockerfile 变了也别自动 `build.py`。 |
| `./scripts/down.py dev` | `docker compose down`。所有卷 + `.data/` 保留。 |
| `./scripts/down.py dev --clear` | `down -v` + 清掉 `.data/{workspaces,workspace-meta,projects}`。 |
| `./scripts/down.py dev --nuclear` | `--clear` + 删平台镜像(`polaris/{api,worker,web}:dev` + workspace runtime 三件套)。 |
| `./scripts/build.py [--only X] [--force] [--push REGISTRY]` | 构建 / 推送 workspace 运行时镜像(dev / stage 共用)。 |

ad-hoc compose 操作:

```bash
docker compose -f compose.dev.yaml logs api -f
docker compose -f compose.dev.yaml logs worker -f
docker compose -f compose.dev.yaml run --rm migrate    # 重新跑 alembic upgrade head
docker compose -f compose.dev.yaml ps
docker compose -f compose.dev.yaml restart web   # vite 重读 vite.config.ts
```

编辑 `apps/api/`、`apps/worker/`、`apps/web/`、或仓库内任何包的源码
都立即生效 —— uvicorn `--reload` 和 Vite HMR 监听 bind-mount 进来的
源码。只有 Dockerfile 或它的 build context(系统包、lockfile)变了
才需要重建镜像。

---

## 4. TLS

Polaris 不带自签 / `localhost` 模式。Traefik 走 Cloudflare 的
DNS-01 ACME,所以:

1. 选一个 Cloudflare DNS 上的域名。
2. 在 `.env.dev` 里写 `POLARIS_DOMAIN`。
3. 向导会校验 `CF_API_TOKEN`(DNS-write 权限),栈拉起后 Traefik
   自己签发并自动续期通配证书。
4. 打开 `https://${POLARIS_DOMAIN}/`(平台根)、
   `https://vnc.${POLARIS_DOMAIN}/`(dev VNC)、
   `https://ide-<hash>.${POLARIS_DOMAIN}/`(每会话 IDE)等。

> **`polaris-dev.xyz` 直接用** —— 维护者持有的 `polaris-dev.xyz` zone
> 的权威 DNS 已经解析到局域网 IP,所以**同一 LAN 内的开发者可以保留
> `POLARIS_DOMAIN=polaris-dev.xyz` 不动**,跳过"注册域名 + 配 CF
> token"那一步。Traefik 仍然对该 zone 的公网 DNS 记录走 Let's
> Encrypt 签发真实证书,HTTPS 照常工作。

`prod.${POLARIS_DOMAIN}` 和 `*.prod.${POLARIS_DOMAIN}` 走单独的发布
平面证书;`*.s3.${POLARIS_DOMAIN}` 给 MinIO virtual-host bucket
寻址。三套证书 Traefik 都从同一个 DNS-01 流程统一管。

---

## 5. 测试

```bash
# 一次性 / pyproject 改了之后跑一次。在仓库根 materialise 共享 .venv/。
uv sync --all-packages --all-extras

uv run --package polaris-api pytest apps/api/tests -v
uv run --package polaris-worker pytest apps/worker/tests -v
uv run --package polaris-design-intent pytest packages/design-intent/tests -v

# scripts/ 自带独立环境(PEP 723 inline deps)。
cd scripts && uv run --group dev pytest

# 前端 type-check + 生产构建跑在 web 容器里。
docker compose -f compose.dev.yaml run --rm web pnpm typecheck
docker compose -f compose.dev.yaml run --rm web pnpm --filter @polaris/web build
```

完整测试矩阵见 [TESTING.md](./TESTING.md)。

---

## 6. 常见开发流程

### 6.1 全部重置

```bash
./scripts/down.py dev --clear         # 交互式,丢掉所有 workspace 状态 + 平台 pg/redis
./scripts/down.py dev --clear --force # 非交互
```

清掉 per-workspace 容器、per-project compose 状态、workspace meta、
Postgres / Redis 卷、registry 数据。**镜像保留**;要一并删用 `--nuclear`。

### 6.2 保留状态停机

```bash
./scripts/down.py dev             # docker compose down,卷 + .data/* 都保留
```

容器拆掉,但所有命名卷 + `.data/` 完整保留。下次 `./scripts/up.py dev`
直接基于现有卷重建容器。

### 6.3 新迁移

```bash
docker compose -f compose.dev.yaml exec api \
    alembic revision --autogenerate -m "add foo"
docker compose -f compose.dev.yaml run --rm migrate
```

Worker 通过同一个 `polaris/api:dev` 镜像的 `apps/api` editable 安装
读表(`apps/worker/entrypoint.sh` 在每次启动时把 editable 路径
re-register 到 bind-mount 路径),不用二次装。

### 6.4 改了 CLI / 模板,重建 workspace 镜像

```bash
./scripts/build.py --only workspace
# 已经在跑的 workspace 容器用旧镜像,新会话才会拿到新镜像。
# `./scripts/down.py dev --clear` 清掉容器,新会话自然用新镜像。
```

`./scripts/build.py --only workspace` 在以下文件变化时重建:
- `infra/workspace/Dockerfile`
- `infra/workspace/polaris-cli/*`(workspace 内的 `polaris` CLI)
- `infra/publish-templates/*`(发布脚手架,COPY 到
  `/opt/polaris-publish-templates`)

### 6.5 日志

| 位置 | 查看方式 |
|---|---|
| api / worker / web | `docker compose -f compose.dev.yaml logs <svc> -f` |
| 每会话 workspace 容器 | `docker logs polaris-ws-<hash>` / `polaris-br-<hash>` |
| Traefik | `docker logs polaris-traefik-1` + `http://localhost:8090/dashboard/` |
| Publish SSE | PublishPanel 的 live-log 区,或 workspace 里 `polaris publish` 的 stdout |

### 6.6 直接连 DB

```bash
docker compose -f compose.dev.yaml exec postgres psql -U root polaris
# 或者从宿主连(postgres 绑在 127.0.0.1 上一个动态端口):
psql "postgresql://root:123456@127.0.0.1:$(docker compose -f compose.dev.yaml port postgres 5432 | cut -d: -f2)/polaris"
```

### 6.7 远程开发?用 dev VNC

`compose.dev.yaml` 里的 `dev-vnc` 是一个预先指向
`https://${POLARIS_DOMAIN}/` 的 chromium 容器。从你笔记本打开
`https://vnc.${POLARIS_DOMAIN}/`,在 chromium 区域里点一下抢控制权
就能用 —— 剪贴板 / 摄像头 API 全好用,因为 Traefik 用同一张通配
证书把它套在 HTTPS 里。这个路由**默认无认证 —— 仅限可信网络**。
要加 gate,在 service 上设 `SELKIES_PASSWORD`。

---

## 7. 故障排查

| 症状 | 常见原因 | 处理 |
|---|---|---|
| `./scripts/up.py dev` 报 Docker 出错 | 守护进程没启 / 当前用户不在 `docker` 组 | 启动 Docker;`sudo usermod -aG docker $USER && exec newgrp docker` |
| Traefik 对根域 / vnc 502 | api / web 容器还在启(uvicorn `--reload`、Vite 冷启) | 等约 10 秒;`docker compose -f compose.dev.yaml logs <svc>` |
| 首次启动 TLS 证书错 | Cloudflare DNS-01 还没完成 | 看 `docker logs polaris-traefik-1`,首次签发约 30–60 秒 |
| `polaris/ide` 构建卡 5 分钟 | 首次 yarn workspaces + Theia compile | 正常,后续走缓存 |
| Session 一直 `queued` | Worker 没跑或崩了 | `docker compose -f compose.dev.yaml logs worker -f`;`docker compose -f compose.dev.yaml restart worker` |
| IDE iframe 一直 "等待代理" | Codex 没调 `set_project_root` | `docker logs polaris-ws-<hash>` 看 Codex transcript,通常是 scaffold 崩了 |
| Workspace 容器报 auth 错 | 宿主 `~/.codex/auth.json` 缺失 | 宿主跑一次 `codex login`,再 `./scripts/down.py dev --clear && ./scripts/up.py dev` |

---

## 相关文档

- [STAGING.zh.md](./STAGING.zh.md) —— 受控环境下的 staging 部署(DNS / TLS / 加固注意)
- [ARCHITECTURE.md](./ARCHITECTURE.md) —— 系统设计
- [API.md](./API.md) —— REST + SSE 端点
- [CONFIGURATION.md](./CONFIGURATION.md) —— 完整环境变量手册
- [FRONTEND.md](./FRONTEND.md) —— React 架构
- [TESTING.md](./TESTING.md) —— 验证流程
