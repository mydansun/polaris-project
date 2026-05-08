/**
 * Per-event-kind body renderers for the non-compact "detail" row of a
 * ChatBubble.  Each `*Body` component takes the event's `payload_jsonb`
 * and produces its own expanded content (code accordion, diff, markdown,
 * etc.).  `renderItemBody` is the one-shot dispatcher.
 */

import { lazy, Suspense } from "react";
import { useTranslation } from "react-i18next";
import { cn, Tabs, TabsContent, TabsList, TabsTrigger } from "@polaris/ui";
import type { EventResponse } from "@polaris/shared-types";

import {
  commandSummary,
  readNumber,
  readString,
} from "./itemVisuals";

// Lazy-load the markdown renderer so react-markdown + remark-gfm (~50KB
// gzip) don't land in the initial bundle.  First agent_message paints
// plain text for a beat while the chunk streams in, then re-renders
// with formatting.
const MarkdownMessage = lazy(() =>
  import("../MarkdownMessage").then((m) => ({ default: m.MarkdownMessage })),
);

// ── Primitives shared across bodies ───────────────────────────────────────

/** Collapsible <details> that wraps a monospace <pre>.  Default-collapsed. */
export function CodeAccordion({
  title,
  body,
  lang,
  defaultOpen = false,
}: {
  title: string;
  body: string;
  lang?: "diff" | "text";
  defaultOpen?: boolean;
}) {
  return (
    <details className="group mt-2" open={defaultOpen}>
      <summary className="flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-text-muted hover:text-text-primary">
        <span className="icon-[mdi--chevron-right] transition-transform group-open:rotate-90" />
        <span>{title}</span>
      </summary>
      <pre
        className={cn(
          "mt-1 max-h-64 overflow-auto rounded-md border border-border-light bg-surface-alt px-2 py-1.5 text-[11px] leading-[1.45] text-text-primary font-mono",
          lang === "diff" && "whitespace-pre",
        )}
      >
        {lang === "diff" ? renderDiff(body) : body}
      </pre>
    </details>
  );
}

/** Highlight unified-diff +/- lines; leave everything else plain. */
function renderDiff(body: string) {
  const lines = body.split("\n");
  return lines.map((line, i) => {
    let color = "";
    if (line.startsWith("+++") || line.startsWith("---")) color = "text-text-muted";
    else if (line.startsWith("+")) color = "text-emerald-600";
    else if (line.startsWith("-")) color = "text-rose-600";
    else if (line.startsWith("@@")) color = "text-cyan-600";
    return (
      <span key={i} className={color ? `block ${color}` : "block"}>
        {line || "\u00A0"}
      </span>
    );
  });
}

// ── Body renderers (one per event kind that has rich content) ─────────────

export function CommandExecutionBody({
  payload,
}: {
  payload: Record<string, unknown>;
}) {
  const command = commandSummary(payload.command);
  const output = readString(payload.output);
  const exit = readNumber(payload.exit_code);

  // Everything useful about a command is already visible in the title
  // (truncated command) + status dot.  The accordion holds the details.
  const parts: string[] = [];
  if (command !== null) parts.push(`$ ${command}`);
  if (typeof exit === "number") parts.push(`\nexit ${exit}`);
  if (output !== null) parts.push("\n" + output);
  const body = parts.join("\n").trim();
  if (!body) return null;

  const accordionTitle =
    typeof exit === "number" && exit !== 0
      ? `Command, exit ${exit}, output`
      : "Command and output";

  return <CodeAccordion title={accordionTitle} body={body} />;
}

type FileChange = {
  path?: string;
  kind?: string;
  move_path?: string | null;
  diff?: string;
  additions?: number;
  deletions?: number;
};

export function FileChangeBody({ payload }: { payload: Record<string, unknown> }) {
  const changes = (payload.changes as FileChange[] | undefined) ?? [];
  if (changes.length === 0) return null;

  const accordionTitle =
    changes.length === 1 ? "Path and diff" : `${changes.length} files`;

  return (
    <details className="group mt-2">
      <summary className="flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-text-muted hover:text-text-primary">
        <span className="icon-[mdi--chevron-right] transition-transform group-open:rotate-90" />
        <span>{accordionTitle}</span>
      </summary>
      <div className="mt-1 flex flex-col gap-3">
        {changes.map((c, i) => {
          const label =
            c.kind === "add" ? "add" : c.kind === "delete" ? "delete" : "update";
          return (
            <div key={`${c.path ?? i}-${i}`} className="flex flex-col gap-1">
              <div className="flex items-center gap-2 text-[11px]">
                <span className="shrink-0 rounded bg-surface-alt px-1 py-0.5 uppercase tracking-wide text-text-muted">
                  {label}
                </span>
                <code className="min-w-0 flex-1 truncate text-[11px] text-text-primary">
                  {c.path ?? "(unnamed)"}
                </code>
                {c.move_path ? (
                  <>
                    <span className="shrink-0 text-text-muted">→</span>
                    <code className="min-w-0 flex-1 truncate text-[11px] text-text-primary">
                      {c.move_path}
                    </code>
                  </>
                ) : null}
              </div>
              {c.diff ? (
                <pre className="max-h-64 overflow-auto whitespace-pre rounded-md border border-border-light bg-surface-alt px-2 py-1.5 font-mono text-[11px] leading-[1.45] text-text-primary">
                  {renderDiff(c.diff)}
                </pre>
              ) : (
                <div className="text-[11px] text-text-muted">(no diff)</div>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}

export function ToolCallBody({ payload }: { payload: Record<string, unknown> }) {
  const server = readString(payload.server);
  const tool = readString(payload.tool);
  const args = payload.arguments;
  const parts: string[] = [];
  const label = [server, tool].filter((v): v is string => v !== null).join(" / ");
  if (label) parts.push(label);
  if (args !== undefined && args !== null) {
    parts.push(typeof args === "string" ? args : JSON.stringify(args, null, 2));
  }
  if (parts.length === 0) return null;
  return <CodeAccordion title="Details" body={parts.join("\n")} />;
}

export function ErrorBody({ payload }: { payload: Record<string, unknown> }) {
  const detail = readString(payload.detail) ?? readString(payload.message);
  if (detail === null) return null;
  return (
    <div className="rounded-md border border-error/30 bg-error-light px-2 py-1 text-[12px] leading-5 text-error">
      {detail}
    </div>
  );
}

export function PlanBody({ payload }: { payload: Record<string, unknown> }) {
  const { t } = useTranslation();
  const tech = readString(payload.text);
  const plain = readString(payload.text_plain);
  if (tech === null && plain === null) return null;

  const card =
    "mt-2 overflow-hidden rounded-xl border border-border-light bg-surface-subtle";
  const body =
    "px-4 py-3 text-[13px] leading-6 text-text-primary [&_p]:my-2 " +
    "[&_h1]:mt-3 [&_h1]:mb-2 [&_h1]:text-[15px] [&_h1]:font-semibold " +
    "[&_h2]:mt-3 [&_h2]:mb-1.5 [&_h2]:text-[14px] [&_h2]:font-semibold " +
    "[&_h3]:mt-2 [&_h3]:mb-1 [&_h3]:text-[13px] [&_h3]:font-semibold " +
    "[&_ul]:my-2 [&_ol]:my-2 [&_li]:my-0.5";
  const fallback =
    "whitespace-pre-wrap break-words text-[13px] leading-6 text-text-primary";

  // No overview available → plain card, single body, no tabs.
  if (plain === null) {
    return (
      <div
        className={card}
        data-testid="plan-card"
        data-semantic-kind="plan_card"
        data-plan-tabs="false"
      >
        <div className={body} data-testid="plan-body-tech">
          <Suspense fallback={<div className={fallback}>{tech!}</div>}>
            <MarkdownMessage text={tech!} />
          </Suspense>
        </div>
      </div>
    );
  }

  // Both available → tab header + swappable body.
  return (
    <div
      className={card}
      data-testid="plan-card"
      data-semantic-kind="plan_card"
      data-plan-tabs="true"
    >
      <Tabs defaultValue="plain">
        <div className="flex items-center justify-end border-b border-border-light bg-white px-3 py-1.5">
          <TabsList className="h-8">
            <TabsTrigger
              value="plain"
              className="px-3 py-1 text-xs"
              data-testid="plan-tab-plain"
              data-semantic-kind="plan_tab_brief"
            >
              <span className="icon-[mdi--book-open-page-variant-outline] mr-1.5 text-sm" />
              {t("items.planTabs.plain")}
            </TabsTrigger>
            <TabsTrigger
              value="tech"
              className="px-3 py-1 text-xs"
              data-testid="plan-tab-tech"
              data-semantic-kind="plan_tab_detailed"
            >
              <span className="icon-[mdi--code-braces] mr-1.5 text-sm" />
              {t("items.planTabs.technical")}
            </TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="plain" className={cn(body, "mt-0")}>
          <Suspense fallback={<div className={fallback}>{plain}</div>}>
            <MarkdownMessage text={plain} />
          </Suspense>
        </TabsContent>
        <TabsContent value="tech" className={cn(body, "mt-0")}>
          <Suspense fallback={<div className={fallback}>{tech ?? ""}</div>}>
            <MarkdownMessage text={tech ?? ""} />
          </Suspense>
        </TabsContent>
      </Tabs>
    </div>
  );
}

export function MoodBoardBody({ payload }: { payload: Record<string, unknown> }) {
  const { t } = useTranslation();
  const url = readString(payload.mood_board_url);
  if (!url) return null;
  return (
    <div
      className="mt-2 w-full max-w-md overflow-hidden rounded-xl border border-border-light bg-surface-subtle"
      data-testid="mood-board-card"
      data-semantic-kind="mood_board"
    >
      <div className="flex items-center gap-1.5 border-b border-border-light bg-white px-3 py-1.5 text-[11px] font-medium uppercase tracking-wide text-text-muted">
        <span className="icon-[mdi--palette-outline] text-sm" />
        {t("items.moodBoard")}
      </div>
      <div className="p-2">
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="block overflow-hidden rounded-md"
        >
          <img
            src={url}
            alt={t("items.moodBoard")}
            loading="lazy"
            className="block h-auto w-full"
            data-testid="mood-board-img"
          />
        </a>
      </div>
    </div>
  );
}


type ReferencesRef = {
  id: string;
  title: string;
  blurred_url: string;
  score?: number | null;
  score_reason?: string | null;
};

/** Discovery references gallery — morphs through three phases:
 *
 *   phase=searching → only a small spinner caption ("正在搜索灵感")
 *   phase=scoring   → grid of blurred S3 thumbs (staggered fade-in),
 *                     each tile gets a spinner overlay
 *   phase=scored    → spinners removed, chosen tile gets an accent
 *                     ring + score badge
 *
 * The grid uses fixed ``aspect-[4/3]`` tiles + ``object-cover`` so
 * Pinterest's wildly varying source aspects normalize visually. */
export function ReferencesBody({ payload }: { payload: Record<string, unknown> }) {
  const { t } = useTranslation();
  const phase = readString(payload.phase) ?? "searching";
  const refsRaw = Array.isArray(payload.refs) ? payload.refs : [];
  const refs: ReferencesRef[] = refsRaw
    .map((r): ReferencesRef | null => {
      if (typeof r !== "object" || r === null) return null;
      const o = r as Record<string, unknown>;
      const id = readString(o.id);
      const url = readString(o.blurred_url);
      if (id === null || url === null) return null;
      return {
        id,
        title: readString(o.title) ?? "",
        blurred_url: url,
        score: typeof o.score === "number" ? o.score : null,
        score_reason: readString(o.score_reason),
      };
    })
    .filter((x): x is ReferencesRef => x !== null);
  const chosenId = readString(payload.chosen_id);

  if (phase === "searching") {
    return (
      <div className="mt-2 flex items-center gap-2 text-[12px] text-text-muted">
        <span className="icon-[lucide--loader-2] animate-spin text-sm" aria-hidden />
        <span>{t("items.referencesSearching")}</span>
      </div>
    );
  }

  if (refs.length === 0) {
    return (
      <div className="mt-2 text-[12px] text-text-muted">
        {t("items.referencesNoneFound")}
      </div>
    );
  }

  const isScoring = phase === "scoring";
  const isScored = phase === "scored";

  return (
    <div className="mt-2 w-full max-w-md overflow-hidden rounded-xl border border-border-light bg-surface-subtle">
      <div className="flex items-center justify-between border-b border-border-light bg-white px-3 py-1.5 text-[11px] font-medium uppercase tracking-wide text-text-muted">
        <span className="flex items-center gap-1.5">
          <span className="icon-[mdi--image-search-outline] text-sm" />
          {t("items.referencesFoundN", { count: refs.length })}
        </span>
        {isScoring && (
          <span className="flex items-center gap-1.5 normal-case text-[10px] font-normal text-accent">
            <span className="icon-[lucide--loader-2] animate-spin text-xs" aria-hidden />
            {t("items.referencesScoring")}
          </span>
        )}
      </div>
      <div className="grid grid-cols-3 gap-1.5 p-2">
        {refs.map((ref, i) => {
          const isChosen = isScored && chosenId !== null && ref.id === chosenId;
          return (
            <div
              key={ref.id}
              title={ref.score_reason ?? ref.title}
              className={cn(
                "relative aspect-[4/3] overflow-hidden rounded-md border border-border-light bg-white",
                "enter-stagger",
                isChosen && "ring-2 ring-accent ring-offset-1 ring-offset-surface-subtle",
              )}
              style={{ ["--stagger-i" as string]: i }}
            >
              <img
                src={ref.blurred_url}
                alt=""
                loading="lazy"
                className="block h-full w-full object-cover"
              />
              {isScoring && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/20 backdrop-blur-[1px]">
                  <span
                    className="icon-[lucide--loader-2] animate-spin text-2xl text-white drop-shadow"
                    aria-hidden
                  />
                </div>
              )}
              {isScored && typeof ref.score === "number" && (
                <span
                  className={cn(
                    "absolute right-1 top-1 rounded-full px-1.5 py-0.5 font-mono text-[9px] font-semibold leading-none",
                    isChosen ? "bg-accent text-white" : "bg-black/60 text-white",
                  )}
                >
                  {ref.score.toFixed(1)}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}


export function ReasoningBody({ payload }: { payload: Record<string, unknown> }) {
  const summary = readString(payload.summary) ?? readString(payload.content);
  if (summary === null) return null;
  return (
    <div className="whitespace-pre-wrap break-words text-[12px] leading-5 text-text-muted">
      {summary}
    </div>
  );
}

export function AgentMessageBody({ payload }: { payload: Record<string, unknown> }) {
  const text = readString(payload.text);
  if (text === null) return null;
  return (
    <Suspense
      fallback={
        <div className="whitespace-pre-wrap break-words text-[13px] leading-6 text-text-primary">
          {text}
        </div>
      }
    >
      <MarkdownMessage text={text} />
    </Suspense>
  );
}

/** One-shot dispatcher — pick the body renderer by event kind. */
export function renderItemBody(item: EventResponse) {
  const p = item.payload_jsonb;
  switch (item.kind) {
    case "codex:agent_message":     return <AgentMessageBody payload={p} />;
    case "codex:plan":              return <PlanBody payload={p} />;
    case "codex:reasoning":         return <ReasoningBody payload={p} />;
    case "codex:command_execution": return <CommandExecutionBody payload={p} />;
    case "codex:file_change":       return <FileChangeBody payload={p} />;
    case "codex:mcp_tool_call":     return <ToolCallBody payload={p} />;
    case "codex:dynamic_tool_call": return <ToolCallBody payload={p} />;
    case "codex:web_search":        return null;
    case "codex:error":             return <ErrorBody payload={p} />;
    case "discovery:references":    return <ReferencesBody payload={p} />;
    case "discovery:moodboard":     return <MoodBoardBody payload={p} />;
    default:                        return null;
  }
}
