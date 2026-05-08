# Polaris

Polaris 是一个面向终端用户的 AI 全栈 app 构建平台。平台把自然语言
请求变成可工作的代码、真实浏览器验证过的行为、Git 备份的版本,以及
一键 Docker + Traefik 路由的部署 —— 落到 `<uuid>.prod.${POLARIS_DOMAIN}`。

第一条消息走 **discovery agent**(LangGraph:clarifier → references →
brief compiler → mood board generator),产出 design brief 和生成的
mood board,然后 Codex 接手。后续消息直接走 Codex 的 plan / build mode。

## Quick Start

```sh
./scripts/up.py            # 提示 dev | stage,首次跑会触发向导
./scripts/up.py dev        # 显式 dev(Vite HMR + uvicorn --reload)
./scripts/up.py stage      # 显式 stage(nginx 服务静态包,无 --reload)
```

打开 `https://${POLARIS_DOMAIN}`(默认建议:`polaris-dev.xyz`)。
首次登录需要邀请码(向导里设);dev 主机上可以点 **Dev Login**
跳过邮箱验证。

> **关于 `polaris-dev.xyz`** —— 这个 zone 的权威 DNS 已经把记录指向
> 维护者所在的局域网 IP,所以**同一局域网的开发者可以直接使用,
> 不需要自己注册或配 DNS**。LAN 之外的部署得用一个属于你的、托在
> Cloudflare DNS 上的域名(Traefik 的 DNS-01 ACME 直接对你的 zone
> 跑)。

每个 mode 拥有自己的 `.env.<mode>` 和 `compose.<mode>.yaml`,跑在自己
的 compose project 名下(`polaris` vs `polaris-stage`),命名卷各自
独立 —— 同一台主机上两套栈可以共存,互不干扰。

## 主机依赖

| 工具 | 用途 |
|------|------|
| **Docker**(Engine 或 Desktop) | 一切跑在容器里;`up.py` 会强制要求 |
| **uv** ≥ 0.11 | 解析 `scripts/*.py` 的 PEP 723 inline-deps |
| **Cloudflare DNS 上的域名** | TLS 走 ACME DNS-01;不支持 localhost / 自签。LAN 内开发者可借用 `polaris-dev.xyz`(DNS 已指向 LAN);其它部署需要属于你的 zone。 |
| **Codex CLI** + `codex login` | 把 Codex auth.json 持久化在宿主;挂进 workspace 用 |

不需要系统 Python venv,不需要系统 pnpm,不需要系统 Make。
api / worker / web 的源码改动通过 bind-mount 生效;`--reload` /
Vite HMR 实时拾取,无需重建镜像。

## 日常命令

所有命令都接收一个可选的 `dev | stage` 位置参数;省略时脚本会提示
(或在 `--non-interactive` 下默认 `dev`)。

| 命令 | 用途 |
|------|------|
| `./scripts/up.py [dev\|stage]` | 必要时配置 + 启动栈。改了 `.env.<mode>` 之后再跑一次。 |
| `./scripts/up.py <mode> --reconfigure` | `.env.<mode>` 完整也强制走向导。 |
| `./scripts/up.py <mode> --non-interactive` | CI 模式 —— 必填项缺失立刻报错。 |
| `./scripts/down.py [dev\|stage]` | 停栈 + 扫掉动态 workspace 容器(保留数据)。 |
| `./scripts/down.py <mode> --clear` | 删平台卷 + 清掉 `.data/{workspaces,workspace-meta,projects}`。 |
| `./scripts/down.py <mode> --nuclear` | `--clear` + 删该 mode 的镜像(api/worker/web)+ workspace/ide/chromium-vnc。 |
| `./scripts/build.py` | 构建 workspace runtime 镜像(幂等,Dockerfile 没变就跳过)。dev/stage 共用。 |
| `./scripts/build.py --force` | 不看 mtime,全量重建。 |
| `./scripts/build.py --push REGISTRY` | 构建后 tag + push 到远端 registry。 |

ad-hoc compose 操作(`logs`、`exec`、`ps`):

```sh
docker compose -f compose.dev.yaml logs api -f
docker compose -f compose.dev.yaml exec api alembic upgrade head

# stage 同形:换文件即可
docker compose -f compose.stage.yaml logs api -f
```

小贴士:在 shell rc 里 `export COMPOSE_FILE=compose.dev.yaml`(或
`compose.stage.yaml`),省掉每次都打 `-f`。

### 远程开发?用 dev VNC

`compose.dev.yaml` 自带一个 `dev-vnc` chromium 容器,在远程开发盒
开发时可以从笔记本看实时前端。容器启动时 chromium 已经打开
`https://${POLARIS_DOMAIN}/`,所以 `apps/web` 的 HMR / live-reload
立刻可见。

```sh
# 笔记本上:
open https://vnc.${POLARIS_DOMAIN}/      # 比如 https://vnc.polaris-dev.xyz/
```

笔记本浏览器加载 Selkies WebRTC UI;在 chromium 区域里点一下抢控制
权。Traefik 用同一张通配证书代理 —— 剪贴板 / 手柄 / 摄像头 API
全好用,因为是真正的 HTTPS secure context。**这条路由本身没鉴权,
仅限可信网络**。要更广暴露,在 service 上设 `SELKIES_PASSWORD`。

## 跑测试

仓库根的 uv workspace 把所有 Python 包绑在一起 —— 不再有
per-package `.venv` 目录。第一次跑会在仓库根 materialise 一个共享
`.venv/`(gitignored)。

```sh
uv sync --all-packages --all-extras       # 一次性 / pyproject 改了之后

uv run --package polaris-api pytest apps/api/tests
uv run --package polaris-worker pytest apps/worker/tests
uv run --package polaris-design-intent pytest packages/design-intent/tests

# scripts/ 自带独立环境(PEP 723 inline deps + uv)。
cd scripts && uv run --group dev pytest
```

前端 type-check + 生产构建跑在 web 容器里:

```sh
docker compose -f compose.dev.yaml run --rm web pnpm typecheck
docker compose -f compose.dev.yaml run --rm web pnpm --filter @polaris/web build
```

宿主侧直接跑 pnpm(比如 `pnpm add foo` 加依赖)只要装了 pnpm 也能用;
host `node_modules/` 故意保留是为了 VSCode / Cursor 的 TypeScript
IntelliSense 还能工作。

## 配置

设置存在仓库根的 `.env.dev` 和 `.env.stage`,各 mode 一个。第一次
`./scripts/up.py <mode>` 会触发向导,逐字段提示并在线校验 token。
`--reconfigure` 强制重跑向导,适合切域名 / 换 TLS / 轮换 key —— 不用
改代码。

字段元数据(默认值 / 必填 / 是否 secret / 校验器)集中在
`scripts/lib/spec.py` —— 这是向导、README、CI 非交互检查的唯一来源。

两个 `.env.<mode>` 都 gitignored。仓库挪位置:

```sh
./scripts/down.py dev
mv polaris-project ~/work/polaris
cd ~/work/polaris
./scripts/up.py dev    # 能跑 —— 所有宿主 bind-mount 都用 `./` 相对路径
```

## 仓库形状

```
apps/
  web/           React 工作台(chat + Theia IDE / Chromium VNC)
  api/           FastAPI 控制平面(鉴权、项目、session、MCP、发布)
  worker/        后台 session 跑器(Redis 消费、discovery + Codex)
packages/
  ide/            定制 Theia IDE
  agent-core/     PolarisCodexSession
  design-intent/  LangGraph discovery agent
  ui/             共享 React 原语
  shared-types/   共享 TS API / SSE 契约
  welcome-page/   chromium-vnc 用的静态欢迎页
infra/
  workspace/     polaris/workspace Dockerfile + workspace 侧 polaris CLI
  chromium/      polaris/chromium-vnc Dockerfile + nginx CDP 代理
  traefik/       静态 + 动态配置(CF DNS-01 ACME,无宿主证书)
  minio/         (compose 里的 service;数据落在这里)
  publish-templates/  per-stack Dockerfile + compose + polaris.yaml 脚手架
scripts/
  build.py / up.py / down.py    上面那 3 个 CLI
  lib/                          validators、env io、wizard、paths、docker_ops
  tests/                        pytest 套件(uv run --group dev pytest)
compose.dev.yaml   Dev 栈(Vite HMR + uvicorn --reload + 源码 bind-mount)
compose.stage.yaml Stage 栈(nginx 静态包,无 --reload,project 名 polaris-stage)
```

## 文档

- [README · English](./README.md)
- [开发](./docs/DEVELOPMENT.zh.md) · [English](./docs/DEVELOPMENT.md)
- [Staging](./docs/STAGING.zh.md) · [English](./docs/STAGING.md)
- [架构](./docs/ARCHITECTURE.zh.md) · [English](./docs/ARCHITECTURE.md)
- [API 参考](./docs/API.zh.md) · [English](./docs/API.md)
- [配置](./docs/CONFIGURATION.zh.md) · [English](./docs/CONFIGURATION.md)
- [前端](./docs/FRONTEND.zh.md) · [English](./docs/FRONTEND.md)
- [路线图](./docs/ROADMAP.zh.md) · [English](./docs/ROADMAP.md)
- [测试](./docs/TESTING.zh.md) · [English](./docs/TESTING.md)
