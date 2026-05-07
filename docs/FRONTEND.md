# Frontend Architecture

## Stack

| Layer | Technology |
|-------|-----------|
| Framework | React 19 |
| Build | Vite 7 |
| Styling | Tailwind CSS v4 + tw-animate-css |
| Fonts | Inter Variable + JetBrains Mono Variable (@fontsource) |
| Components | shadcn/ui (Radix UI) in `packages/ui` (includes Tabs used by PlanBody) |
| Icons | Iconify CSS (`@iconify/tailwind4`) |
| i18n | react-i18next + i18next (en + zh) |
| Types | `@polaris/shared-types` |

## Layout

Two-column flex layout with resizable split:
- **Left**: Chat pane (always visible)
- **Right**: Browser / IDE / hidden (toggle group in header)
- **Divider**: 8px draggable bar with overlay preview during drag
- Split percentage persisted in `localStorage("polaris-split-pct")`
- Min widths: left 280px, right 280px

## Component Tree

```
App
├── LoginPage             (email → invite code (if new) → verification code)
├── ChatPane              (left pane)
│   ├── Header            (logo + project name + deploy 🚀 + status + avatar + ⋮ menu + toggle group)
│   ├── ScrollArea        (message list with noise cluster folding)
│   │   ├── ChatBubble[]  (per-event rendering; body renderers in chat/ChatBubbleBodies.tsx)
│   │   │   ├── AgentMessageBody  (streaming markdown)
│   │   │   ├── PlanBody          (shadcn Tabs: Overview vs Details)
│   │   │   ├── MoodBoardBody     (inline card with generated mood board image)
│   │   │   ├── CommandExecutionBody, FileChangeBody, ToolCallBody, …
│   │   ├── NoiseCluster  (collapsed noise items: "Execute command ×3, Reasoning ×2")
│   │   ├── ClarificationCard  (structured questions from agent — discovery OR codex)
│   │   └── Plan approval (Proceed button after plan mode turn)
│   ├── Working indicator ("Polaris 工作中" pill + spinner)
│   ├── Restart Dialog    (shadcn Dialog, replaces window.confirm)
│   └── Input form        (Ctrl/Cmd+Enter to send; shortcut hint; Stop button)
├── ProjectSwitcher       (left Sheet drawer)
├── PublishPanel          (right Sheet drawer — "Publish now" button + rollback history)
├── EditorPane            (right pane — Theia IDE iframe; labeled "代码编辑器 / Code editor" in UI)
├── BrowserPane           (right pane — Selkies VNC iframe + MCP overlay)
├── QuotaDialog           (shadcn Dialog — shown when POST /sessions returns HTTP 429; distinguishes global vs per-user cap)
└── ExampleProjectCards   (welcome-screen only — 4 cards that send a localized prompt on click; hidden once a project is selected)
```

## i18n

- **Languages**: English (`en.json`) + Simplified Chinese (`zh.json`)
- **Init**: `apps/web/src/i18n/index.ts` — auto-detects browser language, persists choice in localStorage
- **Switch**: Language toggle in header dropdown menu
- **Pattern**: `useTranslation()` hook in components, `i18n.t()` for non-component contexts
- **Scope**: All user-visible strings (buttons, placeholders, errors, status labels, menu items)
- **Not translated**: aria-labels, API errors, agent-generated chat content

## Right Pane Toggle

Three-state toggle group in header:
- **Browser** 🌐 — VNC iframe
- **代码编辑器 / Code editor** </> — Theia iframe (auto-selected when `set_project_root` fires).  The i18n key is still `chat.tabs.ide` for back-compat, but all user-visible strings show "代码编辑器" / "Code editor".
- **Hide** 👁‍🗨 — right pane hidden, chat fills viewport

Auto-switch rules:
- `set_project_root` SSE event → switch to IDE (once per project)
- `browser_focus_requested` SSE event (emitted by Codex's `focus_browser`
  dynamic tool before playwright calls) → switch to Browser

## Session Modes

First message of a project auto-routes through **discovery** (for the
design brief + mood board); every subsequent message goes straight to
`build_direct` so the agent iterates without another plan / proceed
round.  The Proceed button inside the discovery-produced plan card
also sends `build_direct` when clicked.

| Mode | When | Codex collaboration mode |
|---|---|---|
| `discover_then_build` | `sessions.length === 0` for the project | `plan` (after discovery finishes) |
| `build_direct` | Default for 2nd+ messages AND for the Proceed-on-plan button | `default` |

`build_planned` still exists on the backend (default when `mode` is
omitted) but the frontend never sends it — it's reserved for scripted
callers that want a plan round on every turn.

## Chat Event Kinds → Visuals

`apps/web/src/chat/itemVisuals.ts` maps event kinds to icon + title.

| Event kind | Title (zh / en) | Notes |
|---|---|---|
| `discovery:clarifying` | 构思澄清 / Clarifying | Card for structured questions |
| `discovery:references` | 搜索设计参考图 / Searching design references | Internal Pinterest step (name intentionally generic) |
| `discovery:compiled` | 设计方案 / Design brief | Finalized brief text |
| `discovery:moodboard` | 设计灵感板 / Design mood board | `MoodBoardBody` renders the generated PNG inline |
| `codex:plan` | 规划中 / Planning | `PlanBody` with Overview / Details tabs |
| `codex:agent_message` / `codex:file_change` / ... | (various) | |
| `codex:other` + unmapped kinds | 代理活动 / Agent activity | Fallback title for future Codex item types |

## Clarification Cards

Both discovery's `clarifier_ask` and Codex's `request_user_input` flow
through the same SSE kind (`clarification_requested`) and render as
`ClarificationCard`.

- Questions with 2-3 options + free-text override
- Submit → `POST /clarify/response` → Redis → agent returns
- Page refresh recovers via `GET /clarify/pending`
- Color-choice questions render real swatches (discovery attaches `swatch` hex)

## Plan Approval

When a `build_planned` session ends with a plan (not a full build), the
`PlanBody` renders a **Tabs** card (shadcn `@radix-ui/react-tabs` in
`packages/ui`):

- **Overview** (default) — non-technical rewrite from `codex_plan_plain_model`
- **Details** — original technical plan text

Frontend shows a Proceed button under the plan; clicking creates a new
session with `mode: "build_direct"` and a localized trigger message
(`i18n.t("app.proceedWithPlan")`).

## Chat Features

- **Noise clustering**: consecutive command/reasoning/tool items collapsed between meaningful items
- **Session pagination**: load 3 most recent sessions, scroll-to-top loads older
- **Instant initial scroll**: no visible animation on page load
- **Submit**: Ctrl/Cmd+Enter. Textarea enabled during agent working.
- **MCP overlay**: blocks VNC during Playwright tool calls (400ms debounce)
- **Empty agent_message suppression**: interrupted sessions don't show empty bubbles
- **Mood board card**: `MoodBoardBody` caps at `max-w-md`; clicking opens the full S3 URL in a new tab
- **StatusBar throttle**: `useSessionEventHandler.ts` applies a leading-edge-with-trailing-flush throttle (~400ms) on top of the worker's own 500ms server-side coalesce, so a low-frequency burst of single-delta `session_stats_updated` frames renders as one "+N" animation instead of a chain of "+1" flashes.
- **Stop button**: sends `POST /sessions/{id}/interrupt`; `App.tsx::handleInterrupt` optimistically merges the returned `SessionResponse` into local state so the UI flips immediately, before the SSE terminal frame arrives.
- **Example project cards** (welcome screen only): four cards — golf landing page, minimalist todo (LocalStorage), blog (Next.js + PostgreSQL), real-estate booking (Next.js + PostgreSQL).  Clicking a card sends the localized prompt as the first session message (always `discover_then_build`).

## Workspace Restart

shadcn Dialog confirmation → frontend polls `getWorkspaceRuntime()` every 2s until ready.

## Resizable Split

- Drag divider: full-screen overlay with colored panels (white left + stone-100 right) + icons
- Mouse up applies final percentage
- Persisted in localStorage
