# Staging 部署

把 Polaris 部署到专用主机上,**绑定到你自己的域名**,整个项目部署在
**UID 1000 用户的家目录**下 —— 这样 Docker 卷的 bind mount 不用再
chown 对齐。

本文只覆盖这两个维度:

1. 把平台从默认的 `polaris-dev.xyz` 改绑到你自己的域名 —— 涉及的
   `.env.stage` 字段。
2. staging 主机特有的运维注意(加固、备份、故障特征)。

`./scripts/up.py stage` 启动 `compose.stage.yaml`(nginx 静态前端、
api 关掉 `--reload`、compose project 名 `polaris-stage`),Traefik 自
己跑 DNS-01 ACME 签证书。日常命令形状跟
[DEVELOPMENT.zh.md](./DEVELOPMENT.zh.md) 一样,只是每次 `up.py` /
`down.py` 后面带上 `stage` 而已。

---

## ⚠️ 不支持生产级部署

**本项目目前不支持生产级别的部署。** 几个安全边界尚未探明和加固:

- Traefik 面板 `:8090` 无认证。
- 本地 Docker registry `127.0.0.1:5000` 无认证(绑 loopback,不能
  对外暴露)。
- Workspace 容器以**读写方式**挂载宿主 `~/.codex/auth.json` ——
  所有用户共用一个 Codex 账号。
- 平台 api + worker 容器通过 bind-mount 进来的
  `/var/run/docker.sock` 直接驱动宿主 docker daemon 起 workspace +
  已发布项目的 compose 栈。**宿主级 docker 访问权等同于 root**。
  Workspace 容器自己**没**挂 socket —— `polaris dev-up postgres` 等
  命令是经由平台 API 代为调用 docker。Traefik 只读挂载 socket 做
  服务发现。
- Publish pipeline 跑用户生成的 compose,和平台共用同一台宿主机,
  防护仅限 "`ports:` 去毒"。容器逃逸 / 嘈杂邻居都没有 docker 默认
  以外的防御。
- `POLARIS_MAX_*_RUNS` 控成本,**不是**安全边界。
- `POLARIS_INVITE_CODE` 是注册的唯一门禁。泄了就等于任何人能起
  workspace。

**建议**:仅限受控环境 —— 内部试吃、可信协作者、firewall 到已知 IP
的 CI / demo 主机。**不要让不信任流量打到 Polaris 实例上。**

---

## 1. 改绑你自己的域名

线上系统里所有域名引用都靠 `${POLARIS_DOMAIN}` (以及 `prod.` /
`s3.` 派生)在 compose 阶段插值;Traefik 通过 Cloudflare DNS-01
自动签三组证书,**没有需要手动维护的证书文件**。

假设你的域名是 `example.com`,三个区要解析到 staging 主机的公网 IP
(闭环可以用 LAN IP):

| 区 | 用途 |
|---|---|
| `example.com` + `*.example.com` | 平台根(web + `/api`)和每会话 IDE / browser 子域 |
| `prod.example.com` + `*.prod.example.com` | 已发布的用户项目(`<uuid>.prod.example.com`) |
| `s3.example.com` + `*.s3.example.com` | MinIO(path-style + virtual-host bucket 寻址) |

### 1.1 `.env.stage` —— 所有跟域名相关的字段

```bash
# 平台域名(compose.dev.yaml 里所有 Traefik 标签靠 ${POLARIS_DOMAIN}
# 插值;agent prompt + compose label 渲染也读这个)。
POLARIS_DOMAIN=example.com

# 发布平面 —— 每个项目上线到 <uuid>.prod.example.com
POLARIS_PROD_DOMAIN_BASE=prod.example.com

# Web 用 FRONTEND_URL 写签名 cookie,CORS 必须匹配。
FRONTEND_URL=https://example.com
POLARIS_CORS_ORIGINS=["https://example.com"]

# 写入 DB 的每 workspace 公共 URL 模板,前端读这个值。
POLARIS_IDE_PUBLIC_URL_TEMPLATE=https://ide-{workspaceHash}.example.com
POLARIS_BROWSER_PUBLIC_URL_TEMPLATE=https://browser-{workspaceHash}.example.com

# S3 / MinIO —— MinIO 对外就是这两个 URL。
S3_ENDPOINT=https://s3.example.com
S3_URL_BASE=https://polaris.s3.example.com

# Pinterest MCP —— 你自己的实例。
POLARIS_PINTEREST_TOOL_BASE=http://pinterest-mcp.internal:9801

# 前端编译时读;保持相对路径就跟域名无关(Traefik 路由 /api/* → api:8000)。
VITE_API_BASE_URL=/api
```

### 1.2 Cloudflare DNS-01 token

Traefik 通过 Cloudflare DNS-01 自动签所有证书 —— **宿主机不跑
`certbot`**,**没有 `/etc/letsencrypt/` 要挂载**。准一个权限范围是
父 zone(以及 `prod.` / `s3.` 在不同 zone 的话也加上)
**DNS:Edit** 的 API token,放进 `.env.stage`:

```bash
CF_API_TOKEN=<cloudflare DNS-edit token>
ACME_EMAIL=admin@example.com
```

`./scripts/up.py stage` 在拉起栈之前会在线校验 token。首次签发每张
证书约 30–60 秒,续期自动跑。

### 1.3 其它 secrets

```
SESSION_SECRET=<openssl rand -hex 48>
POLARIS_INVITE_CODE=<任意字符串>                     # 注册门禁
# Staging 上这两项务必**留空** —— 它们启用一键 dev-login(绕过邮件
# 验证码)。留空时 /auth/dev-login 会 404,前端的 "Dev Login" 按钮
# 也会隐藏。
POLARIS_DEV_USER_EMAIL=
POLARIS_DEV_USER_NAME=
OPENAI_SECRET=sk-...                                # discovery / clarifier / mood board 必须
POSTMARK_SERVER_TOKEN=<postmark token>              # 验证码发送;留空验证码会打到 api stdout
POSTMARK_MESSAGE_STREAM=outbound
POSTMARK_FROM_EMAIL=noreply@example.com
S3_ACCESS_KEY_ID=polaris
S3_SECRET_ACCESS_KEY=<openssl rand -hex 32>         # MinIO root password 同值,向导自己复用
S3_BUCKET=polaris
POLARIS_MAX_GLOBAL_RUNS=6
POLARIS_MAX_USER_RUNS=2
POLARIS_CODEX_TURN_TIMEOUT_SECONDS=900
```

完整参考:[CONFIGURATION.md](./CONFIGURATION.md)。

---

## 2. 部署位置:以 UID 1000 用户运行

部署到**宿主 UID 1000 用户的家目录**。两个原因:

- **`/opt/` 默认归 root**。平台要读写 `.data/`(已发布项目状态、
  证书、项目 archive)和仓库根下的 per-workspace meta —— root 拥有
  的路径只会增加摩擦。
- **Workspace / IDE 容器镜像以 UID 1000 运行**(见
  `infra/workspace/Dockerfile` `USER 1000` + `packages/ide/Dockerfile`
  `USER 1000`)。这些容器 bind-mount 的宿主路径(workspace 卷、
  `~/.codex/auth.json`、mood board 写入)在宿主侧也是 UID 1000 拥有
  时,权限自然对齐,不需要额外 chown。

大多数云 VM 上第一个交互式用户就是 UID 1000(Ubuntu 的 `ubuntu`、
Debian 的 `admin`、Fedora 的 `fedora`),**不需要**另建账号,验证
并复用即可:

```bash
id -u                                              # 应输出 1000
groups | grep -qw docker && echo "docker group OK"
# 没在 docker 组:sudo usermod -aG docker $USER && exec newgrp docker
```

以这个用户 clone + 配置:

```bash
cd ~
git clone <repo> polaris-2
cd polaris-2
cp .env.example .env.stage                         # 按 §1 填
chmod 600 .env.stage                               # secret 都在里面
codex login                                        # workspace 容器要 bind-mount ~/.codex/auth.json
```

---

## 3. 拉起栈

```bash
./scripts/up.py stage          # 首次启动会触发交互式向导,起 stage 栈
./scripts/build.py             # 构建 polaris/{ide,workspace,chromium-vnc}:latest
docker compose -f compose.stage.yaml exec api alembic upgrade head
```

CI / 自动化场景用 `./scripts/up.py stage --non-interactive` —— 任何
必填项缺失立刻报错,绝不弹问。

Traefik 把证书签出来之后做健康检查:

```bash
curl https://example.com/api/health   # {service: "polaris-api", status: "ok"}
curl https://example.com/api/ready    # {database: "ok", redis: "ok"}
```

---

## 4. 升级流程

```bash
cd ~/polaris-2
git pull
./scripts/build.py             # 只重建过期的 workspace 镜像
./scripts/up.py stage          # 重建并重启 api/worker/web 容器
docker compose -f compose.stage.yaml exec api alembic upgrade head
```

`./scripts/up.py stage` 走 `docker compose up -d --build`,只重建
镜像或配置变了的服务。`apps/*`、`packages/*` 的源码对 api/worker
仍是 bind-mount 进容器,但 stage 把 api 的 `--reload` 关掉了,所以
代码改了要 `docker compose -f compose.stage.yaml restart api` 才生效。
前端是烘焙进 `polaris/web:stage` 的,改了前端要
`docker compose -f compose.stage.yaml build web && up -d`。

Workspace 运行时镜像重建**不影响**正在跑的用户容器,它们用旧镜像跑
到下次新会话为止。要全局刷新:重启服务前先 `./scripts/down.py stage --clear`。

---

## 5. 备份和恢复

备份要写到异地(另一台主机、S3、或你环境允许的任何目的地)。
staging 单机仍然是单点故障。

### Postgres

```bash
docker compose -f compose.stage.yaml exec -T postgres \
  pg_dump -U root -d polaris > ~/backups/polaris-$(date +%F).sql
# 恢复:
docker compose -f compose.stage.yaml exec -T postgres psql -U root -d polaris \
  < ~/backups/polaris-<date>.sql
```

### MinIO

MinIO 数据是 bind-mount 到 `infra/minio/data/`(归 MinIO 容器 UID
所有)。用一次性容器做快照,不用停 MinIO:

```bash
docker run --rm \
  -v $HOME/polaris-2/infra/minio/data:/data:ro \
  -v $HOME/backups:/out \
  alpine tar -czf /out/minio-$(date +%F).tgz -C /data .
```

### 已发布项目状态

每个已发布项目独立目录 `~/polaris-2/.data/projects/<uuid>/`:

- `archives/<short-hash>.tar.gz` —— 每个版本的冻结源码
- `secrets.env` —— 每项目 DB 密码 / session 密钥
- `compose.prod.yml` + `compose.polaris.yml` —— 当前线上 compose

备份整个 `.data/projects/`。恢复后,已经在跑的 prod 容器继续
跑(镜像在本地 registry + 容器层 cache 里),下一次对该项目
`compose up` 时读回恢复后的状态。

### Redis

瞬态,不用备份。丢了只是 in-flight session 无法 resume,新 session
正常。

### Cron(可选)

```bash
mkdir -p ~/backups
(crontab -l 2>/dev/null; cat <<'EOF'
0 3 * * * docker compose -f $HOME/polaris-2/compose.stage.yaml exec -T postgres pg_dump -U root -d polaris > ~/backups/polaris-$(date +\%F).sql
10 3 * * * docker run --rm -v $HOME/polaris-2/infra/minio/data:/data:ro -v $HOME/backups:/out alpine tar -czf /out/minio-$(date +\%F).tgz -C /data .
30 3 * * * find ~/backups -mtime +14 -delete
EOF
) | crontab -
```

(用户 cron 里直接跑 `docker exec` / `docker run` 因为部署用户在
`docker` 组里。)

---

## 6. 运维

### 6.1 日志

| 来源 | 位置 |
|---|---|
| api | `docker compose -f compose.stage.yaml logs api -f` |
| worker | `docker compose -f compose.stage.yaml logs worker -f` |
| web | `docker compose -f compose.stage.yaml logs web -f` |
| 每会话 workspace 容器 | `docker logs polaris-ws-<hash>` / `polaris-br-<hash>` |
| 已发布容器 | `docker logs polaris-pub-<projid>-web-1` |
| Publish pipeline | DB `deployments.build_log` / `smoke_log`;SSE 推到 `GET /deployments/{id}/events`;workspace 里 `polaris publish` 的 stdout |
| Traefik | `docker logs polaris-traefik-1` + `http://<host>:8090/dashboard/` |

### 6.2 常见故障特征

| 症状 | 原因 | 从哪查 |
|---|---|---|
| api / worker 容器反复重启 | `.env.stage` 错 / secret 缺 | `docker compose -f compose.stage.yaml logs api`(能抓到启动 traceback) |
| Traefik 对根域 404 | `${POLARIS_DOMAIN}` 改了但 compose 标签是创建时烘焙的 | `.env.stage` 改完跑 `./scripts/down.py stage && ./scripts/up.py stage` 让标签重新生成 |
| Traefik 对 `ide-*.example.com` 404 | Workspace 容器崩了或没加入 `polaris-shared` 网络 | `docker logs polaris-ws-<hash>` |
| Session 一直 queued | Worker 崩了 | `docker compose -f compose.stage.yaml ps worker` + tail 日志 |
| Publish 报 `smoke probe never succeeded` | 用户容器启动阶段崩;真正原因在 web 容器日志里(pipeline 自动追加到 `smoke_log`) | PublishPanel live log "captured tail of `<svc>` container logs" 区段 |
| 用户一窝堆在 "queued" | `POLARIS_MAX_GLOBAL_RUNS` 上限 | 在 `.env.stage` 调大,跑 `./scripts/up.py stage` 让 api / worker 重建 |

### 6.3 清空

```bash
./scripts/down.py stage --clear         # 交互式,丢所有 workspace 状态 + 平台 pg/redis
./scripts/down.py stage --clear --force # 非交互
```

`--clear` 保留构建的镜像和本地 registry。要一并清掉(所有 polaris
容器 + 所有卷 + 所有 bind-mount 数据目录,只留 `~/.codex/auth.json`):

```bash
./scripts/down.py stage --nuclear
```

### 6.4 保留状态停机

```bash
./scripts/down.py stage
```

容器拆掉,所有命名卷和 `.data/` 完整保留。下次 `./scripts/up.py stage`
基于现有卷秒级重建。

---

## 7. 开放前加固清单

在把这台 staging 主机指向任何人之前:

- **开启宿主防火墙,入站只放行 80 和 443。** 平台绑的其它端口
  (8090 Traefik 面板、5000 本地 registry、5432 Postgres)要么无
  鉴权,要么只能走 loopback,不应对外。

  ufw 的话:

  ```bash
  sudo ufw default deny incoming
  sudo ufw default allow outgoing
  sudo ufw allow 22/tcp              # SSH —— 生产可进一步限到管理员 IP
  sudo ufw allow 80/tcp
  sudo ufw allow 443/tcp
  sudo ufw enable
  sudo ufw status verbose
  ```

  firewalld 的等价配置:

  ```bash
  sudo firewall-cmd --set-default-zone=public
  sudo firewall-cmd --permanent --add-service=http
  sudo firewall-cmd --permanent --add-service=https
  sudo firewall-cmd --permanent --add-service=ssh
  sudo firewall-cmd --reload
  ```

  云厂商安全组同样在基础设施层镜像这条策略(双保险)。

- `chmod 600 ~/polaris-2/.env.stage` —— 所有凭据都在里面;再
  `chmod 700 ~` 防其他本地用户跨读。

- 轮换 `POLARIS_INVITE_CODE`,怀疑泄露就换,置空作为紧急关闸。

- 定时备份 —— 每天至少 Postgres dump + MinIO 卷快照到异地存储。

- 监控 Traefik 面板 + `docker stats`,盯失控的 workspace 容器。
  并发靠 `POLARIS_MAX_*_RUNS` 限制。

- 把邀请码当 admin 凭据分发。

这份清单**不覆盖**(也是为什么 staging 是推荐上限,见文档顶警告):
容器逃逸防御、per-workspace 资源配额、per-user Codex 凭据、带鉴权的
docker registry、Traefik 面板鉴权、租户间网络隔离。这些都是需要
先做设计才能对不信任用户开放 Polaris 的前置课题。

---

## 相关文档

- [DEVELOPMENT.zh.md](./DEVELOPMENT.zh.md) —— 本地开发(`./scripts/up.py`、Dev Login、热重载)
- [ARCHITECTURE.md](./ARCHITECTURE.md) —— 系统设计 / 数据模型 / publish pipeline
- [API.md](./API.md) —— REST + SSE 端点
- [CONFIGURATION.md](./CONFIGURATION.md) —— 完整环境变量手册
- [FRONTEND.md](./FRONTEND.md) —— React 架构
- [TESTING.md](./TESTING.md) —— 验证流程
- `infra/traefik/README.md` —— Traefik 细节(路由 / 证书布局)
