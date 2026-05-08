# 前端架构

## 技术栈

| 层 | 技术 |
|---|---|
| 框架 | React 19 |
| 构建 | Vite 7 |
| 样式 | Tailwind CSS v4 + tw-animate-css |
| 字体 | Inter Variable + JetBrains Mono Variable(@fontsource) |
| 组件 | shadcn/ui(Radix UI),封装在 `packages/ui`(含 PlanBody 用的 Tabs) |
| 图标 | Iconify CSS(`@iconify/tailwind4`) |
| i18n | react-i18next + i18next(en + zh) |
| 类型 | `@polaris/shared-types` |

## 布局

两栏 flex,可拖动中分线:
- **左栏**:Chat 面板(常驻)
- **右栏**:Browser / IDE / 隐藏(header 里的 toggle group)
- **分界条**:8px 可拖,拖动时全屏 overlay 预览
- 分隔比持久化到 `localStorage("polaris-split-pct")`
- 最小宽度:左 280px,右 280px

## 组件树

```
App
├── LoginPage             (邮箱 → 邀请码(若新)→ 验证码)
├── ChatPane              (左栏)
│   ├── Header            (logo + 项目名 + deploy 🚀 + 状态 + 头像 + ⋮ 菜单 + toggle group)
│   ├── ScrollArea        (消息列表 + 噪声折叠)
│   │   ├── ChatBubble[]  (按 event 渲染;body 渲染器在 chat/ChatBubbleBodies.tsx)
│   │   │   ├── AgentMessageBody  (流式 markdown)
│   │   │   ├── PlanBody          (shadcn Tabs:Overview / Details)
│   │   │   ├── MoodBoardBody     (内联卡片,展示生成的 mood board)
│   │   │   ├── CommandExecutionBody / FileChangeBody / ToolCallBody / …
│   │   ├── NoiseCluster  (折叠的噪声项:"Execute command ×3, Reasoning ×2")
│   │   ├── ClarificationCard  (来自 agent 的结构化追问 —— discovery 或 codex)
│   │   └── Plan approval (plan-mode 之后的 Proceed 按钮)
│   ├── Working 指示器     ("Polaris 工作中" 标签 + spinner)
│   ├── Restart Dialog    (shadcn Dialog 确认)
│   └── 输入栏             (Ctrl/Cmd+Enter 发送;快捷键提示;Stop 按钮)
├── ProjectSwitcher       (左侧 Sheet 抽屉)
├── PublishPanel          (右侧 Sheet 抽屉 —— "立即发布" 按钮 + 回滚历史)
├── EditorPane            (右栏 —— Theia IDE iframe;UI 标签 "代码编辑器 / Code editor")
├── BrowserPane           (右栏 —— Selkies VNC iframe + MCP overlay)
├── QuotaDialog           (shadcn Dialog —— POST /sessions 返回 HTTP 429 时弹;区分全局上限和单用户上限)
└── ExampleProjectCards   (仅欢迎屏 —— 4 张卡片,点击发本地化的首条 prompt;选定项目后隐藏)
```

## i18n

- **语言**:英文(`en.json`)+ 简体中文(`zh.json`)
- **初始化**:`apps/web/src/i18n/index.ts` —— 自动探测浏览器语言,
  在 localStorage 持久化用户选择
- **切换**:header 下拉菜单里的语言切换
- **使用模式**:组件里用 `useTranslation()` hook,非组件上下文用 `i18n.t()`
- **覆盖范围**:所有用户可见文本(按钮、占位符、错误、状态标签、菜单项)
- **不翻译**:aria-labels、API 错误、agent 生成的对话内容

## 右栏切换

header 里的三态 toggle group:
- **Browser** 🌐 —— VNC iframe
- **代码编辑器 / Code editor** </> —— Theia iframe(`set_project_root` 触发时自动选中)。
  i18n key:`chat.tabs.ide` → "Code editor" / "代码编辑器"。
- **Hide** 👁‍🗨 —— 右栏隐藏,chat 全屏

自动切换规则:
- `set_project_root` SSE 事件 → 切到 IDE(每项目一次)
- `browser_focus_requested` SSE 事件(Codex 的 `focus_browser` 动态工具
  在调 playwright 之前推) → 切到 Browser

## Session Modes

项目第一条消息自动走 **discovery**(产出 design brief + mood board);
后续每条消息直接走 `build_direct`,agent 不再多走一轮 plan / proceed。
discovery 产生的 plan 卡片里的 Proceed 按钮被点时也发 `build_direct`。

| Mode | 何时 | Codex collaboration mode |
|---|---|---|
| `discover_then_build` | 项目 `sessions.length === 0` | `plan`(discovery 完成后) |
| `build_direct` | 第二条以上消息 + plan 上的 Proceed 按钮的默认 | `default` |

`build_planned` 是后端在 `mode` 省略时的默认值;前端不发 ——
留给想每轮都 plan 一下的脚本调用方。

## Chat 事件类型 → 视觉

`apps/web/src/chat/itemVisuals.ts` 把 event kind 映射到图标 + 标题。

| Event kind | 标题(zh / en) | 备注 |
|---|---|---|
| `discovery:clarifying` | 构思澄清 / Clarifying | 结构化追问的卡片 |
| `discovery:references` | 搜索设计参考图 / Searching design references | 内部走 Pinterest(命名故意中性) |
| `discovery:compiled` | 设计方案 / Design brief | 最终化的 brief 文本 |
| `discovery:moodboard` | 设计灵感板 / Design mood board | `MoodBoardBody` 内联渲染生成的 PNG |
| `codex:plan` | 规划中 / Planning | `PlanBody` 带 Overview / Details tabs |
| `codex:agent_message` / `codex:file_change` / ... | (各异) | |
| `codex:other` + 未映射类型 | 代理活动 / Agent activity | 给未来 Codex item type 的兜底标题 |

## Clarification 卡片

discovery 的 `clarifier_ask` 和 Codex 的 `request_user_input` 都走同一种
SSE kind(`clarification_requested`),前端渲染成 `ClarificationCard`。

- 每题 2-3 个选项 + 自由文本兜底
- 提交 → `POST /clarify/response` → Redis → agent 返回
- 页面刷新通过 `GET /clarify/pending` 恢复
- 配色题渲染真实色卡(discovery 在选项上挂 `swatch` hex)

## Plan 批准

`build_planned` session 以一个 plan 收尾(不是完整 build)时,
`PlanBody` 渲染一张 **Tabs** 卡片(shadcn `@radix-ui/react-tabs`,在
`packages/ui`):

- **Overview**(默认) —— `codex_plan_plain_model` 重写的非技术化版本
- **Details** —— 原始技术性 plan 文本

前端在 plan 下方展示 Proceed 按钮;点击后创建一个 `mode: "build_direct"`
的新 session,触发消息走本地化(`i18n.t("app.proceedWithPlan")`)。

## Chat 特性

- **噪声折叠**:有意义的项之间连续的 command / reasoning / tool 项
  会被折叠
- **Session 分页**:加载最新 3 条 session,滚到顶部加载更老的
- **首屏即时滚动**:页面加载时不显式动画
- **提交**:Ctrl/Cmd+Enter。agent 工作时输入框可用。
- **MCP overlay**:Playwright 工具调用时遮挡 VNC(400ms debounce)
- **空 agent_message 抑制**:被 interrupt 的 session 不显示空气泡
- **Mood board 卡片**:`MoodBoardBody` 上限 `max-w-md`;点击在新 tab
  打开完整 S3 URL
- **StatusBar throttle**:`useSessionEventHandler.ts` 在 worker 自己
  500ms 服务端合并的基础上,再加一层 leading-edge-with-trailing-flush
  throttle(~400ms),所以低频但连发的单 delta `session_stats_updated`
  会渲染成一次 "+N" 动画,而不是一串 "+1" 闪烁。
- **Stop 按钮**:发 `POST /sessions/{id}/interrupt`;`App.tsx::handleInterrupt`
  乐观合并返回的 `SessionResponse` 到本地 state,UI 翻状态早于 SSE
  终止帧到达。
- **Example project cards**(仅欢迎屏):四张卡片 —— golf landing
  page、极简 todo(LocalStorage)、blog(Next.js + PostgreSQL)、
  real-estate 预订(Next.js + PostgreSQL)。点击把本地化 prompt 作
  为首条 session 消息发出(总是 `discover_then_build`)。

## Workspace 重启

shadcn Dialog 确认 → 前端每 2 秒轮询 `getWorkspaceRuntime()` 直到 ready。

## 可拖动中分线

- 拖动:全屏 overlay(白左 + stone-100 右)+ 图标
- 鼠标抬起时落实最终百分比
- 持久化到 localStorage
