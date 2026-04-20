# Staging 部署

把 Polaris 部署到专用主机上,**绑定到你自己的域名**,api / worker /
web 以长驻服务形式跑(**Supervisor**,自动重启,不带 `--reload`),
整个项目部署在 **UID 1000 用户的家目录**下 —— 这样 Docker 卷的
bind mount 不用再 chown 对齐。

本文只覆盖这两个维度:

1. 把平台从默认的 `polaris-dev.xyz` 改绑到你自己的域名 —— 涉及的
   环境变量 + DNS / TLS。
2. api / worker / web 以非-dev 形态长期运行(Supervisor),由宿主
   的 UID 1000 用户运行。
3. staging 主机特有的运维注意。

本地开发(`make dev`、Dev Login、热重载、自签证书)见
[DEVELOPMENT.zh.md](./DEVELOPMENT.zh.md)。

---

## ⚠️ 不支持生产级部署

**本项目目前不支持生产级别的部署。** 几个安全边界尚未探明和加固:

- Traefik 面板 `:8090` 无认证。
- 本地 Docker registry `127.0.0.1:5000` 无认证(绑 loopback,不能
  对外暴露)。
- Workspace 容器以**读写方式**挂载宿主 `~/.codex/auth.json` ——
  所有用户共用一个 Codex 账号。
- 平台 api + worker(以 Supervisor 托管的宿主进程形式运行)直接
  驱动宿主 Docker daemon 起 workspace 容器 + 已发布项目的 compose
  栈。**宿主级 Docker 访问权等同于 root**。workspace 容器本身**没**
  挂 `/var/run/docker.sock`,`polaris dev-up postgres` 等命令是经由
  平台 API 代为调用 Docker,不是容器内直连。Traefik 只读挂载 socket
  做服务发现,不等同于 root。
- Publish pipeline 跑用户生成的 compose,和平台共用同一台宿主机,
  防护仅限"`ports:` 去毒"。容器逃逸 / 嘈杂邻居都没有 Docker 默认
  以外的防御。
- `POLARIS_MAX_*_RUNS` 控成本,**不是**安全边界。
- `POLARIS_INVITE_CODE` 是注册的唯一门禁。泄了就等于任何人能起
  workspace。

**建议**:仅限受控环境 —— 内部试吃、可信协作者、firewall 到已知 IP
的 CI / demo 主机。**不要让不信任流量打到 Polaris 实例上。**

---

## 1. 改绑你自己的域名

假设你的域名是 `example.com`,四个域下要解析到 staging 主机的公网 IP
(闭环可以用 LAN IP):

| 域 | 用途 |
|---|---|
| `example.com` | 平台根(web + `/api`) |
| `*.example.com` | 每会话 IDE / browser 子域 |
| `prod.example.com` + `*.prod.example.com` | 已发布的用户项目 |
| `*.s3.example.com` + `s3.example.com` | MinIO |

### 1.1 `.env` —— 所有跟域名相关的字段

```bash
# 平台域名(agent prompt + compose label 渲染用)
POLARIS_DOMAIN=example.com

# 发布平面 —— 每个项目上线到 <uuid>.prod.example.com
POLARIS_PROD_DOMAIN_BASE=prod.example.com

# Web 用 FRONTEND_URL 写签名 cookie,CORS 必须匹配
FRONTEND_URL=https://example.com
POLARIS_CORS_ORIGINS=["https://example.com"]

# 写入 DB 的每 workspace 公共 URL 模板,前端读这个值
POLARIS_IDE_PUBLIC_URL_TEMPLATE=https://ide-{workspaceHash}.example.com
POLARIS_BROWSER_PUBLIC_URL_TEMPLATE=https://browser-{workspaceHash}.example.com

# S3 / MinIO —— MinIO 对外就是这两个 URL
S3_ENDPOINT=https://s3.example.com
S3_URL_BASE=https://polaris.s3.example.com

# Pinterest MCP —— 你自己的实例
POLARIS_PINTEREST_TOOL_BASE=http://pinterest-mcp.internal:9801

# 前端编译时读,保持相对路径就跟域名无关(Traefik 路由 /api/* → :8000)
VITE_API_BASE_URL=/api
```

### 1.2 Let's Encrypt 证书

三对(通配 SAN 只能匹配一级 label,每个发布 / 平台 / S3 区各一对):

```bash
sudo certbot certonly --manual --preferred-challenges dns \
  -d example.com -d "*.example.com"
sudo certbot certonly --manual --preferred-challenges dns \
  -d prod.example.com -d "*.prod.example.com"
sudo certbot certonly --manual --preferred-challenges dns \
  -d "*.s3.example.com"
```

### 1.3 `.env` 之外的硬编码域名引用

有几处配置默认把域名以字符串形式写死在源码 / 配置里,**不**走 `.env`,
改绑自己域名时得手动同步:

**Traefik 证书路径** —— `infra/traefik/dynamic/certs.yaml`:

```yaml
tls:
  certificates:
    - certFile: /etc/letsencrypt/live/example.com/fullchain.pem
      keyFile:  /etc/letsencrypt/live/example.com/privkey.pem
    - certFile: /etc/letsencrypt/live/prod.example.com/fullchain.pem
      keyFile:  /etc/letsencrypt/live/prod.example.com/privkey.pem
    - certFile: /etc/letsencrypt/live/s3.example.com/fullchain.pem
      keyFile:  /etc/letsencrypt/live/s3.example.com/privkey.pem
  stores:
    default:
      defaultCertificate:
        certFile: /etc/letsencrypt/live/example.com/fullchain.pem
        keyFile:  /etc/letsencrypt/live/example.com/privkey.pem
```

**Traefik 路由** —— `infra/traefik/dynamic/main-site.yaml` 一共有
三条 Host 规则 + 一个 www→apex 的重定向中间件,**全部**要改:

- `main-api` router —— `Host(\`example.com\`) && PathPrefix(\`/api/\`)`
- `main-web` router —— `Host(\`example.com\`)`(非 `/api` 的流量转到 nginx sidecar,upstream `http://polaris-web:8080` 走 docker DNS,见 §4.3)
- `main-www-redirect` router —— `Host(\`www.example.com\`)`
- `redirect-www-to-apex` 中间件 —— `regex:` 和 `replacement:` **两处**
  都要改;`\\.` 转义不要合并成 `.`

**MinIO** —— `infra/minio/compose.yaml` 里有两处写死的域名,没有
对应的 env 开关:

- `environment.MINIO_DOMAIN: s3.example.com` —— 启用 MinIO 自己对
  virtual-host 桶路由的识别.不改的话,`<bucket>.s3.example.com`
  请求能到 MinIO,但 MinIO 把整条 host 当作 bucket 名,400 拒绝.
- `traefik.http.routers.minio.rule=HostRegexp(\`^(.+\\.)?s3\\.example\\.com$$\`)`
  —— 这一条路由同时匹配 path-style(`s3.example.com/<bucket>/<key>`)
  和 virtual-host(`<bucket>.s3.example.com`).`\\.` 转义经 YAML 解析
  后到 Traefik 变 `\.`,不能省。

### 1.4 其它 secrets(和 `.env.example` 一致)

```
SESSION_SECRET=<openssl rand -hex 48>
POLARIS_INVITE_CODE=<任意字符串>                     # 注册门禁
# Staging 上这两项务必**留空** —— 它们启用一键 dev-login(绕过邮件
# 验证码).留空时 /auth/dev-login 会 404,前端的 "Dev Login" 按钮
# 也会隐藏.
POLARIS_DEV_USER_EMAIL=
POLARIS_DEV_USER_NAME=
OPENAI_SECRET=sk-...                                # discovery / clarifier / mood board 必须
POSTMARK_SERVER_TOKEN=<postmark token>              # 验证码发送;留空则验证码打到 stdout(staging 不建议)
POSTMARK_MESSAGE_STREAM=outbound
POSTMARK_FROM_EMAIL=noreply@example.com
MINIO_ROOT_USER=root
MINIO_ROOT_PASSWORD=<openssl rand -hex 16>
S3_ACCESS_KEY_ID=polaris
S3_SECRET_ACCESS_KEY=<同 MINIO_ROOT_PASSWORD>
S3_BUCKET=polaris
POLARIS_MAX_GLOBAL_RUNS=6
POLARIS_MAX_USER_RUNS=2
POLARIS_CODEX_TURN_TIMEOUT_SECONDS=900
```

完整参考:[CONFIGURATION.md](./CONFIGURATION.md)。

---

## 2. 部署位置:以 UID 1000 用户运行

部署到**宿主 UID 1000 用户的家目录**。两个原因:

- **`/opt/` 默认归 root**。平台要读写 `.data/`(已发布项目状态)、
  `apps/api/.venv/`、生成的镜像 / bundle、以及仓库根下的 per-workspace
  meta —— root 拥有的路径只会增加摩擦。
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

以这个用户 clone + 配置。本文档使用 `$HOME/polaris-project` 作为
典型路径;下面 supervisor 等处的绝对路径示例写作
`/home/ubuntu/polaris-project` —— **把 `ubuntu` 替换成你 UID 1000
用户的实际名字**。

```bash
cd ~
git clone <repo> polaris-project
cd polaris-project
corepack enable
cp .env.example .env                               # 按 §1 + §1.4 填
chmod 600 .env                                     # secret 都在里面
```

另外 `~/.codex/auth.json` 要提前准备好 —— 以这个用户身份跑一次
`codex login`。Workspace 容器会 bind-mount 这个文件,缺它每个
Codex session 启动都会挂。

---

## 3. 初装

以你的 UID 1000 用户,在仓库根:

```bash
make staging
```

一条链:`bootstrap` → `welcome-page` → `pull-images` → `build-ide` →
`build-workspace` → `build-chromium` → `infra`(postgres / redis /
registry / traefik / minio,带 `--wait`,所以 postgres 健康检查通过
才进下一步)→ `migrate`(alembic upgrade head)→
`pnpm --filter @polaris/web build`(产出 `apps/web/dist/`)→ 拉起
nginx web sidecar(§4.3)。

首次约 10 分钟(主要是 Theia IDE 构建)。`git pull` 之后再跑是依赖
感知的 —— 只重建过期镜像。

**`make staging` 不会启动 api / worker** —— 那是 Supervisor 的事
(§4.1、§4.2)。nginx web sidecar 则**会**被 `make staging` 自动
(重)拉起。不要在 staging 主机上跑 `make dev`,它走前台
`process-compose` TUI,只适合交互式开发。

**镜像重建触发条件**(只有 git pull 到新代码才用得上):

- `polaris/ide` —— `packages/ide/Dockerfile` 或 yarn.lock 改动时
- `polaris/workspace` —— `infra/workspace/Dockerfile`、workspace CLI
  (`infra/workspace/polaris-cli/`),或**任何** `infra/publish-templates/`
  文件改动时
- `polaris/chromium-vnc` —— `infra/chromium/Dockerfile` 或
  `cdp-proxy.conf` 改动时

Makefile 的 target 是依赖感知的 —— 只重建变化的部分。

---

## 4. 用 Supervisor 跑 api / worker / web

Supervisor(`apt-get install supervisor` / `dnf install supervisor`)
是一个简单的进程监管工具,它自己以 root 身份运行,按配置以指定用户
启动子进程,自带崩溃重启 + 日志轮转。这替代了 dev 环境的
`process-compose up`。

### 4.1 API

`/etc/supervisor/conf.d/polaris-api.conf`(以 root 建):

```ini
[program:polaris-api]
command=/home/ubuntu/polaris-project/apps/api/.venv/bin/uvicorn
    polaris_api.main:app
    --host 0.0.0.0 --port 8000
    --workers 2
    --proxy-headers --forwarded-allow-ips=*
directory=/home/ubuntu/polaris-project/apps/api
user=ubuntu
autostart=true
autorestart=true
startsecs=5
startretries=10
stopwaitsecs=30
stopsignal=TERM
redirect_stderr=true
stdout_logfile=/var/log/supervisor/polaris-api.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=5
```

不需要写 `environment=` —— `apps/api/src/polaris_api/config.py` 用
`pydantic-settings` 直接读仓库根的 `.env`,worker 侧同理。
`--workers 2` 安全 —— API 无状态;CPU 允许可以调大。不加 `--reload`。

### 4.2 Worker

`/etc/supervisor/conf.d/polaris-worker.conf`:

```ini
[program:polaris-worker]
command=/home/ubuntu/polaris-project/apps/worker/.venv/bin/polaris-worker
directory=/home/ubuntu/polaris-project/apps/worker
user=ubuntu
autostart=true
autorestart=true
startsecs=10
startretries=10
stopwaitsecs=60
stopsignal=TERM
redirect_stderr=true
stdout_logfile=/var/log/supervisor/polaris-worker.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=5
```

部署用户需要:
- 在 `docker` 组里(§2 已核验) —— worker 通过宿主 docker daemon
  起 workspace + publish 容器。
- 能读自己家目录下的 `~/.codex/auth.json` —— workspace 容器
  bind-mount 用。

### 4.3 Web —— 发 `apps/web/dist/` 的 nginx sidecar

和 dev(`pnpm dev:web`,Vite 热重载)不同,staging 发的是构建产物。
`make staging` 已经在 §3 里打过 `apps/web/dist/`;§5 的升级流程里每次
发版会重打一次。一个 nginx 小容器只读挂出这个目录发就够了。

sidecar 定义在 `infra/web/compose.yaml`,track 进仓库(bind-mount
路径相对 compose 文件目录,所以同一份文件在任何机器上都成立):

```yaml
# infra/web/compose.yaml(节选)
services:
  polaris-web:
    image: nginxinc/nginx-unprivileged:1.27-alpine
    container_name: polaris-web
    user: "1000:1000"
    restart: unless-stopped
    volumes:
      - ../../apps/web/dist:/usr/share/nginx/html:ro
    networks:
      - traefik-public

networks:
  traefik-public:
    name: traefik-public
    external: true
```

为什么要这个非直觉形状:

- **相对 bind-mount**(`../../apps/web/dist`)。Compose 解析卷路径
  是**相对于 compose 文件所在目录**的,所以不管仓库 clone 到
  `/home/ubuntu/polaris-project`、`/home/sun/polaris-prod` 还是别的
  地方,同一份 compose 都工作 —— 不需要每台机器改,不需要 gitignore。
- **`nginxinc/nginx-unprivileged`** 监听 `:8080`,不需要 root(Traefik
  的 `main-web` service upstream 也就指这个端口)。
- **`user: "1000:1000"`** 对齐宿主 `apps/web/dist/` 的属主 UID。
  默认 `nginx` 用户(UID 101)不能穿过属于 UID 1000 的 home 目录,
  即使文件本身 world-readable —— 否则你得去 `chmod o+x /home/<user>`。
- **不绑 `ports:`** —— 容器仅在 `traefik-public` docker 网络可达,
  Traefik 走 docker 内置 DNS 解析 `polaris-web`。不绑宿主端口 →
  不会误泄露到 LAN/公网,也不依赖 `ufw-docker` 的规则同步。

生命周期接到 Makefile 和 `scripts/down.sh`:

- `make staging` —— 跑完 web bundle 构建后(§3)执行
  `docker compose -f infra/web/compose.yaml up -d`。
- `make stop` —— 和其它 polaris 容器一起停,保留状态。dev 主机上
  没起过 sidecar,这一步是 no-op。
- `make down`(`scripts/down.sh`)—— 走全量清理时对 sidecar 跑
  `down -v`。

Traefik 路由已经在 `infra/traefik/dynamic/main-site.yaml` 里 —— router
`main-web` 指 `http://polaris-web:8080`(`traefik-public` 网络内的
docker DNS),`main-api` 指 `http://host.docker.internal:8000`
(api 以 Supervisor 跑在宿主,走 host-gateway)。除非你改绑非默认
域名(§1.3),否则不需要再加 dynamic 配置。

### 4.4 加载 + 启动

```bash
sudo supervisorctl reread                          # 解析新 conf
sudo supervisorctl update                          # 启新定义的 program
sudo supervisorctl status polaris-api polaris-worker
```

健康检查:

```bash
curl https://example.com/api/health                # {service: "polaris-api", status: "ok"}
curl https://example.com/api/ready                 # {database: "ok", redis: "ok"}
```

Per-program 管理:

```bash
sudo supervisorctl restart polaris-api
sudo supervisorctl stop polaris-worker
sudo supervisorctl tail -f polaris-api             # 实时 stdout
sudo supervisorctl tail -f polaris-api stderr
```

---

## 5. 升级流程

```bash
# 以部署用户(UID 1000),在仓库根:
cd ~/polaris-project
git pull
make bootstrap                             # pyproject 有变时重装 venv
make build-workspace                       # infra/workspace 或 publish-templates 变了就重建
pnpm --filter @polaris/web build           # 重新构前端 bundle
make migrate                               # alembic upgrade head(幂等)

# 以 root(或 sudo):
sudo supervisorctl restart polaris-api polaris-worker
# 以部署用户(`docker` 组成员 → compose 不用 sudo):
docker compose -f infra/web/compose.yaml restart polaris-web
```

**Workspace 镜像重建**不影响正在跑的用户容器,它们用旧镜像跑到
下次新会话为止。要全局刷新:重启服务前先 `make clear`。

---

## 6. 备份和恢复

备份要写到异地(另一台主机、S3、或你环境允许的任何目的地)。
staging 单机仍然是单点故障。

### Postgres

```bash
docker exec polaris-project-postgres-1 \
  pg_dump -U root -d polaris > /home/ubuntu/backups/polaris-$(date +%F).sql
# 恢复:
docker exec -i polaris-project-postgres-1 psql -U root -d polaris \
  < /home/ubuntu/backups/polaris-<date>.sql
```

### MinIO

MinIO 的数据用 bind mount 存在 `infra/minio/data/`(**不是**命名卷),
归 MinIO 容器的 UID 所有。用一次性容器做快照就好,不用停 MinIO:

```bash
docker run --rm \
  -v /home/ubuntu/polaris-project/infra/minio/data:/data:ro \
  -v /home/ubuntu/backups:/out \
  alpine tar -czf /out/minio-$(date +%F).tgz -C /data .
```

### 已发布项目状态

每个已发布项目独立目录
`~/polaris-project/.data/projects/<uuid>/`:

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

用户级 cron 就够了:

```bash
mkdir -p ~/backups
(crontab -l 2>/dev/null; cat <<'EOF'
0 3 * * * docker exec polaris-project-postgres-1 pg_dump -U root -d polaris > ~/backups/polaris-$(date +\%F).sql
10 3 * * * docker run --rm -v $HOME/polaris-project/infra/minio/data:/data:ro -v ~/backups:/out alpine tar -czf /out/minio-$(date +\%F).tgz -C /data .
30 3 * * * find ~/backups -mtime +14 -delete
EOF
) | crontab -
```

(用户 cron 里直接跑 `docker exec` / `docker run` 因为部署用户在
`docker` 组里。)

---

## 7. 运维

### 7.1 日志

| 来源 | 位置 |
|---|---|
| api | `sudo supervisorctl tail -f polaris-api`,或 `/var/log/supervisor/polaris-api.log` |
| worker | `sudo supervisorctl tail -f polaris-worker`,或 `/var/log/supervisor/polaris-worker.log` |
| web(nginx sidecar) | `docker logs polaris-web` |
| 每会话 workspace 容器 | `docker logs polaris-ws-<hash>` / `polaris-br-<hash>` |
| 已发布容器 | `docker logs polaris-pub-<projid>-web-1` |
| Publish pipeline | DB 里 `deployments.build_log` / `smoke_log`;SSE 推到 `GET /deployments/{id}/events`;workspace 里 `polaris publish` 的 stdout |
| Traefik | `docker logs polaris-traefik-1` + `http://<host>:8090/dashboard/` |

每条 program 配置里都 `redirect_stderr=true`,stdout + stderr 合并
到同一个 `stdout_logfile`。Supervisor 按 50 MB × 5 份自动轮转。

### 7.2 常见故障签名

| 症状 | 原因 | 从哪查 |
|---|---|---|
| `polaris-api` / `polaris-worker` 处于 `FATAL` 或反复重启 | `.env` 错 / secret 缺 | `sudo supervisorctl tail polaris-api`(能抓到启动 traceback) |
| Traefik 对根域名 404 | Dynamic 配置没重新加载(file provider 监听 `/etc/traefik/dynamic/`) | 在那个目录里动一下文件,Traefik 1 秒内自动拾取 |
| Traefik 对 `ide-*.example.com` 404 | Workspace 容器崩了或没加入 `polaris-internal` 网络 | `docker logs polaris-ws-<hash>` |
| Session 一直 queued | Worker 崩了 | `sudo supervisorctl status polaris-worker` + tail 日志 |
| Publish 报 `smoke probe never succeeded` | 用户容器启动阶段崩;真正原因在 web 容器日志里(pipeline 自动追加到 `smoke_log`) | PublishPanel live log "captured tail of `<svc>` container logs" 区段 |
| 用户一窝堆在 "queued" | `POLARIS_MAX_GLOBAL_RUNS` 上限 | 在 `.env` 调大,`sudo supervisorctl restart polaris-api polaris-worker` |

### 7.3 清空

```bash
make clear              # 交互式,丢所有 workspace 状态 + 平台 pg/redis
make clear FORCE=1      # 非交互
```

**`make clear` 保留**:traefik、MinIO、registry、构建的镜像。要一并
清掉(所有 polaris 容器 + 所有卷 + 所有 bind-mount 数据目录,只留
构建的镜像和 `~/.codex/auth.json`):

```bash
make down               # 核选项,交互式
make down FORCE=1       # 非交互
```

跑 `make down` 之前先停 Supervisor 管的 api / worker
(`sudo supervisorctl stop polaris-api polaris-worker`),否则它们会
因为 Postgres 消失而疯狂重连。

### 7.4 保留状态停机

```bash
sudo supervisorctl stop polaris-api polaris-worker
make stop                                                # 所有 polaris 容器 含 nginx sidecar(workspace / published / infra)
```

`make stop` 只停容器不删 —— nginx web sidecar(当
`infra/web/compose.yaml` 存在时)、每会话 workspace / published /
preview 容器、MinIO、traefik、平台 postgres-redis-registry 全部
就地停住。所有卷、DB 行、已发布项目状态
(`.data/projects/<uuid>/`)、网络定义都原样保留。恢复时按
`make infra` + `sudo supervisorctl start ...` +
`docker compose -f infra/web/compose.yaml start` 原路起来,
或者直接 `make staging`(它会把同样的 up 步骤串起来),容器不重建。

如果你更喜欢让 compose 下次起来时重建容器,`make stop-infra` 会对
MinIO / traefik / 平台 postgres-redis-registry 做 `down`(停 + 删),
卷仍然保留。

---

## 8. 开放前加固清单

在把这台 staging 主机指向任何人之前:

- **开启宿主防火墙,入站只放行 80 和 443。** 平台绑的其它端口
  (8090 Traefik 面板、9001 MinIO 控制台、5000 本地 registry、
  5432 / 6379 Postgres / Redis、8000 / 5173 API / Vite)要么无鉴权,
  要么只能走 loopback / 内网,不应对外。ufw 的话:

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

  云厂商的安全组同样在基础设施层镜像这条策略(双保险)。

- `chmod 600 ~/polaris-project/.env` —— 所有凭据都在里面;
  再 `chmod 700 ~` 防其他本地用户跨读。

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

- [DEVELOPMENT.zh.md](./DEVELOPMENT.zh.md) —— 本地开发(`make dev`、Dev Login、热重载)
- [ARCHITECTURE.md](./ARCHITECTURE.md) —— 系统设计 / 数据模型 / publish pipeline
- [API.md](./API.md) —— REST + SSE 端点
- [CONFIGURATION.md](./CONFIGURATION.md) —— 完整环境变量手册
- [FRONTEND.md](./FRONTEND.md) —— React 架构
- [TESTING.md](./TESTING.md) —— 验证流程
- `infra/traefik/README.md` —— Traefik 细节(路由 / 证书布局)
