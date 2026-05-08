# 录像回放 Fixture

> [English version](./README.md)

回放测试套件用的 fixture。`ReplayCodexSession` 和
`ReplayDesignIntentRunner` 从这里读数据,让 worker 不调用
OpenAI / Pinterest / image-gen 就能走完一遍录制好的场景。

## 目录结构

```
raw/
  _dummy.json            ← 入仓,schema 校验自检用
  *.json[.gz]            ← gitignore,真实录像
annotated/
  _dummy.json            ← 入仓
  *.json                 ← 标注层(有就入仓)
assets/
  .gitkeep               ← 入仓
  *-workspace.tar.gz     ← gitignore,跟 raw fixture 配对的
                           构建后 /workspace 快照
```

## 真实录像为什么 gitignore

录制器原样捕获节点输出,里面会带:

* docker 内网 IP 地址(出现在 vite stderr 里)
* codex 的 `account/rateLimits/updated` 通知里的 `planType`
* `pinterest_refs[].max/.normal` 上的内部 Pinterest 代理 URL

这些**都不是凭证**(已审计),但 ship 纪律是不必要的基础设施
坐标默认就别提交,尤其考虑到仓库以后可能开源。开发者本地录制,
回放测试在 fixture 缺失时优雅 skip。

## 录制(每个场景一次)

```bash
# 1. api + worker 加 recorder 路径,重启
echo 'POLARIS_RECORD=/home/sun/projects/polaris-dev/tests/fixtures/replay/raw/<scenario>.json.gz' >> .env.dev
./scripts/up.py dev

# 2. 在浏览器(或 Playwright MCP)里走一遍场景。每次用户点击应当
#    POST 到 /replay/record/append;codex 帧 + design-intent 节点
#    输出会通过 tap 自动落盘。

# 3. 场景完成后 finalize
curl -sk -X POST https://polaris-dev.xyz/api/replay/record/finalize \
     -H 'Content-Type: application/json' -d '{"cleanup":true}'

# 4. 把构建后的 /workspace 快照打成 tarball 放进 assets/
docker exec polaris-ws-<hash> sh -c \
  'cd /workspace && tar --exclude=./node_modules --exclude=./dist \
                       --exclude=./.git --exclude=./.codex \
                       --exclude=./.playwright-mcp \
                       -czf /tmp/ws.tar.gz .'
docker cp polaris-ws-<hash>:/tmp/ws.tar.gz \
          tests/fixtures/replay/assets/<scenario>-workspace.tar.gz

# 5. 还原 env,重启
sed -i '/^POLARIS_RECORD=/d' .env.dev
./scripts/up.py dev
```

录制完跑一下 `scripts/replay_audit.py <fixture>` 做最小覆盖检查
(三轮澄清、item lifecycle 平衡、turn/completed 终态等)。

## 回放

```bash
# 1. worker 指向本地录像
echo 'POLARIS_REPLAY=/home/sun/projects/polaris-dev/tests/fixtures/replay/raw/<scenario>.json.gz' >> .env.dev
./scripts/up.py dev

# 2. 跑 e2e
POLARIS_E2E_REPLAY=1 pnpm --filter @polaris/web exec \
  playwright test replay-<scenario>

# 3. 还原
sed -i '/^POLARIS_REPLAY=/d' .env.dev
./scripts/up.py dev
```

CI 默认不设 `POLARIS_E2E_REPLAY` → 回放测试自动 skip。
等到需要 CI 跑回放验证时,在 pre-test 步骤先把 fixture 从内部
存储拉下来,再翻这个 env 即可。

## 安全审计快查

录像里**没有**:

* OpenAI / Codex API key、Bearer token、JWT、refresh_token
* 用户 PII(邮箱、用户名、git author)
* chatgpt_account_id / sub / 任何身份标识符
* 第三方服务的 API key(Pinterest / Unsplash / S3)
* 宿主绝对路径(`_sanitize_log` 已替换为 `/workspace`)

录像里**有**(非凭证、非个人识别):

* `planType:plus` —— ChatGPT 订阅档位
* 三个 RFC1918 私网 IP(docker 内网,不可路由)
* 内部 Pinterest 代理域名(访问需 API key,不在 fixture 里)
* 内部 docker DNS 名(`workspace`、`chromium-vnc` 等)

## 已知设计取舍

| 决定 | 原因 |
|---|---|
| 工作区用 tarball seed,不重放 file_change diff | codex 录的是 unified diff,需要 base 文件就位才能 apply。回放不真跑 codex 的脚手架命令,base 不存在。tarball 自包含、可重放、数据小(golf 30 KB) |
| `mood_board_b64` 内联进 fixture | b64 PNG ~3.85 MB,gzip 后 ~600 KB。回放时上传到本地 MinIO 拿到新 URL |
| 录制阶段不抓翻译后的 plan | event_tap 在 codex 帧到达时触发,worker 的 plan→plain 翻译在那之后跑。fixture 里只有技术版 plan,前端 plan 卡片回退成单 tab 渲染 |
| `clarifier_step` 节点输出含全量 messages | LangGraph 的 state 累积模型,n² 增长。golf 场景下 design-intent JSONL 7 MB → gzip 5 MB。后续可以改成只录 delta |

## 工程债与 follow-up

* **fixture schema versioning**:目前 schema 写死 v1,未来 codex
  协议升级时需要 fixture 跟着改,缺一个迁移机制。
* **annotation 层尚未启用**:Phase 2.5 设计了 raw → annotated 的
  语义层(intent / rationale / narrative),实现还没落地。
* **新场景需要再录一次**:目前只有 `golf-landing-page`,加新场景
  得跑一次完整录制,大约 15 分钟、$1-3。
