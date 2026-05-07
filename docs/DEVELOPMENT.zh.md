# 开发

本地开发 Polaris 平台代码时如何搭起来。共享测试 / demo 环境参见
[STAGING.zh.md](./STAGING.zh.md)。

---

## 1. 主机依赖

| 工具 | 用途 | 最低版本 |
|---|---|---|
| Linux 或 macOS | 开发主机 | Ubuntu 22.04+ / macOS 13+ |
| Docker Engine / Desktop | 每会话 workspace + publish 容器 | 24.x+,compose v2 |
| Python | API / worker 的 venv | 3.12+ |
| Node.js + pnpm | 前端 + 共享包 | Node 20+,pnpm(`corepack enable`) |
| process-compose | `make dev` 用的本地进程监管 | 任意 |
| lsof | Makefile 的端口预检 | 任意 |
| Codex CLI + `codex login` | Workspace 容器会 bind-mount 宿主机 `~/.codex/auth.json` | 最新 |

以上任一缺失时 `make prereqs` 会立刻报错。

**`make dev` 占用的端口**(启动前先确认空闲):

| 端口 | 用途 |
|---|---|
| 8000 | FastAPI |
| 5173 | Vite dev server |
| 5432 / 6379 | Postgres / Redis(仅 127.0.0.1) |
| 5000 | 本地 Docker registry(仅 127.0.0.1) |
| 80 / 443 / 8090 | Traefik(只在需要 polaris-dev.xyz 域名访问时使用;`dev-local` 可跳过)。8090 是无认证 dashboard |
| 9001 | MinIO 控制台(绑 0.0.0.0 —— LAN 可见;登录凭 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`) |

`make preflight-ports` 若检测到 8000 或 5173 被占用会直接中止
`make dev`。

---

## 2. 初次搭建

```bash
git clone <repo> && cd polaris-project
corepack enable
cp .env.example .env
```

`.env` 最小必填:

```
SESSION_SECRET=$(openssl rand -hex 48)    # 任意长随机串
POLARIS_INVITE_CODE=dev-invite            # 任意字符串,新用户注册时要用
POLARIS_DEV_USER_EMAIL=dev@polaris.local  # 启用 "Dev Login" 按钮 + /auth/dev-login(留空则两者都关)
POLARIS_DEV_USER_NAME=Polaris Dev         # 自动创建的 dev 用户的显示名
OPENAI_SECRET=                            # 本地开发可留空(跳过 discovery agent)
POSTMARK_SERVER_TOKEN=                    # 留空时验证码打到 API 控制台
```

Postmark 和 OpenAI 都留空的情况下:

- **Dev Login** 可以登录,跳过邮件验证。
- 首条消息走 discovery agent 的链路会在 compiler 步失败(需要
  OpenAI key)。本地工作时可以**从第二条消息**开始聊 —— 后续消息走的
  是纯 codex 分支,不依赖 OpenAI。
- 其它功能(workspace compose、Theia IDE、chromium VNC、Codex
  session、publish pipeline)都不依赖这两个 key。

bootstrap venv + pnpm install + 构建 workspace / IDE / chromium 镜像:

```bash
make bootstrap            # 建 apps/api/.venv、apps/worker/.venv,跑 pnpm install
make build-ide            # 定制版 Theia 基础镜像(首次 5-10 分钟)
make build-workspace      # IDE + dev 工具链 + Codex CLI
make build-chromium       # 带 CDP proxy 的 chromium-vnc
```

这几个 target 是依赖感知的 —— `git pull` 之后只会重建变化的部分。

---

## 3. 启动

### 3.1 完整栈(推荐)

```bash
make dev
```

链式跑:`bootstrap` → `preflight-ports` → `pull-images` →
`build-workspace` → `build-chromium` → `infra`(postgres / redis /
registry / traefik / minio)→ `migrate` → `process-compose up`
(api + worker + web)。

打开 `http://localhost:5173/` 点 **Dev Login**。

### 3.2 跳过 Docker infra

本地已经有自己的 Postgres + Redis(比如 brew 起的),可以跳过 infra
compose:

```bash
make dev-local            # 等同于 `dev` 减去 `make infra`
```

`.env` 里 `POLARIS_DATABASE_URL` / `POLARIS_REDIS_URL` 指向你的实例。

### 3.3 单独跑某个服务

需要聚焦调试某一个进程时:

```bash
make api        # uvicorn --reload
make worker     # polaris-worker(Redis 消费者)
make web        # pnpm dev:web
```

每个 target 会自己先确保 venv 是 bootstrap 过的,不用先跑 `make dev`。

---

## 4. 本地 TLS(可选)

`make dev` 直接走 `http://localhost:5173/` 就够用,只有想走完整 Traefik
路由链路时才需要 TLS。想走的话:

```bash
# 仓库内已经放了自签对 ./certs/*.pem
# 在 /etc/hosts 添加这些域名解析到 127.0.0.1:
#   polaris-dev.xyz, ide-*.polaris-dev.xyz, browser-*.polaris-dev.xyz
```

`/etc/hosts` 不支持通配符,得把正在测试的 `ide-<hash>.polaris-dev.xyz`
/ `browser-<hash>.polaris-dev.xyz` 逐个列出来,或干脆走
`http://localhost:5173/` 绕过 Traefik。

真实 DNS + Let's Encrypt 的流程见 [STAGING.zh.md](./STAGING.zh.md)。

---

## 5. 测试

```bash
cd apps/api && .venv/bin/python -m pytest tests/ -v          # API + CLI audit
make test-worker                                             # worker orchestrator + discovery 取消
make test-design-intent                                      # LangGraph 节点 + palette_step
cd apps/web && pnpm exec tsc --noEmit                        # 前端类型检查
```

完整测试矩阵见 [TESTING.md](./TESTING.md)。

---

## 6. 常见开发流程

### 6.1 全部重置

```bash
make clear                # 交互式,丢弃所有 workspace 状态
make clear FORCE=1        # 非交互式
```

清掉 per-workspace 容器、per-project compose 状态、workspace meta、
Postgres / Redis 的卷。**不清**:已构建的镜像、本地 Docker registry。

### 6.2 保留状态停机

```bash
# 在 process-compose TUI 窗口:Ctrl+C                (停 api / worker / web)
make stop                 # 就地停所有 polaris 容器(容器、卷、.data/*、网络、镜像全保留)
```

四档生命周期速查:

| 命令 | 容器 | 卷 | `.data/*` | 镜像 |
|---|---|---|---|---|
| `make stop` | 停(保留) | 保留 | 保留 | 保留 |
| `make stop-infra` | infra `down`(删) | 保留 | 保留 | 保留 |
| `make clear` | 工作区 `rm -f` + 清;infra pg/redis 停 | 平台 pg/redis 删 | 清空 | 保留 |
| `make down` | **全部**删(含 traefik / minio / registry) | **全部**删(含 minio bind mount) | 清空 | 保留 |

`make stop` 之后 `make dev` 毫秒级原地起;`make stop-infra` 也行,
只是 postgres/redis/traefik/minio/registry 下次 `make infra` 要重新
创建容器,比 `make stop` 慢几秒。

### 6.3 新迁移

```bash
cd apps/api && .venv/bin/alembic revision --autogenerate -m "add foo"
make migrate              # 跑 alembic upgrade head
```

Worker 通过同一个 apps/api venv 读表,不用二次装。

### 6.4 CLI / 模板改了要重建 workspace 镜像

```bash
make build-workspace
# 已经在跑的 workspace 容器用的是旧镜像,新会话才会拿到新镜像
# `make clear` 清掉容器,下次会话会用新镜像
```

`make build-workspace` 在以下文件变化时重建:

- `infra/workspace/Dockerfile`
- `infra/workspace/polaris-cli/*`(workspace 内的 `polaris` CLI)
- `infra/publish-templates/*`(发布脚手架,会 COPY 到
  `/opt/polaris-publish-templates`)

### 6.5 日志

| 位置 | 查看方式 |
|---|---|
| api / worker / web | `process-compose attach`(TUI,per-process tail) |
| 每会话 workspace 容器 | `docker logs polaris-ws-<hash>` / `polaris-br-<hash>` |
| Traefik | `http://localhost:8090/dashboard/` + `docker logs polaris-traefik-1` |
| Publish SSE | PublishPanel 的 live-log 区域,或 workspace 里 `polaris publish` 的 stdout |

### 6.6 直接连 DB

```bash
docker exec -it polaris-project-postgres-1 psql -U root polaris
```

---

## 7. 故障排查

| 症状 | 常见原因 | 处理 |
|---|---|---|
| `make dev` 在 `preflight-ports` 步挂了 | 8000 或 5173 已被占 | `lsof -i :8000` → kill 占用进程 |
| `pnpm install` 提示缺 corepack | Node < 16.10 | 升到 Node 20 LTS |
| `build-ide` 卡 5 分钟 | 首次 Theia 构建要拉 yarn workspaces + 编译 | 正常,后续秒级 |
| Session 一直 queued | worker 没跑 | 看 `process-compose attach` 再重启 worker |
| IDE iframe 一直 "等待代理" | Codex 没调 `set_project_root` | `docker logs polaris-ws-<hash>` 看 Codex transcript,多半是 scaffold 崩了 |
| Workspace 容器报 auth 错 | 宿主机 `~/.codex/auth.json` 缺失 | 宿主机跑一次 `codex login`,再 `make clear && make dev` |

---

## 相关文档

- [STAGING.zh.md](./STAGING.zh.md) — 受控环境下的 staging 部署(DNS / TLS / 加固注意)
- [ARCHITECTURE.md](./ARCHITECTURE.md) — 系统设计
- [API.md](./API.md) — REST + SSE 端点
- [CONFIGURATION.md](./CONFIGURATION.md) — 完整环境变量手册
- [FRONTEND.md](./FRONTEND.md) — React 架构
- [TESTING.md](./TESTING.md) — 验证流程
