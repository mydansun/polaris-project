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
      <div className={card}>
        <div className={body}>
          <Suspense fallback={<div className={fallback}>{tech!}</div>}>
            <MarkdownMessage text={tech!} />
          </Suspense>
        </div>
      </div>
    );
  }

  // Both available → tab header + swappable body.
  return (
    <div className={card}>
      <Tabs defaultValue="plain">
        <div className="flex items-center justify-end border-b border-border-light bg-white px-3 py-1.5">
          <TabsList className="h-8">
            <TabsTrigger value="plain" className="px-3 py-1 text-xs">
              <span className="icon-[mdi--book-open-page-variant-outline] mr-1.5 text-sm" />
              {t("items.planTabs.plain")}
            </TabsTrigger>
            <TabsTrigger value="tech" className="px-3 py-1 text-xs">
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
    <div className="mt-2 w-full max-w-md overflow-hidden rounded-xl border border-border-light bg-surface-subtle">
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
          />
        </a>
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
    case "discovery:moodboard":     return <MoodBoardBody payload={p} />;
    default:                        return null;
  }
}
