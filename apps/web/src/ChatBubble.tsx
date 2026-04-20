/**
 * ChatBubble — single source of truth for rendering one chat row.
 *
 * Heavy lifting is split into sibling files:
 *   - chat/itemVisuals.ts       — KIND_VISUALS + itemTitle + compactDetailText
 *   - chat/ChatBubbleMeta.tsx   — MetaStatus + StatusDot + formatTime
 *   - chat/ChatBubbleBodies.tsx — per-kind body renderers (*Body + renderItemBody)
 *
 * Back-compat: re-exports `KIND_VISUALS`, `itemTitle`, `readString` so
 * existing imports from `./ChatBubble` keep working.
 */

import i18n from "./i18n";
import { Avatar, AvatarFallback, Card as _Card, CardContent as _CardContent, cn } from "@polaris/ui";
import type { EventResponse } from "@polaris/shared-types";

import {
  AgentMessageBody,
  renderItemBody,
} from "./chat/ChatBubbleBodies";
import { MetaStatus, formatTime } from "./chat/ChatBubbleMeta";
import {
  COMPACT_KINDS,
  KIND_VISUALS,
  compactDetailText,
  itemTitle,
  readString,
} from "./chat/itemVisuals";

// ── Re-exports (back-compat for other files that import from ChatBubble) ──
export { KIND_VISUALS, itemTitle, readString };

// ── Public types ──────────────────────────────────────────────────────────

export type ChatMessage =
  | { id: string; role: "user"; kind: "text"; text: string; timestamp: string }
  | {
      id: string;
      role: "agent";
      kind: "item";
      item: EventResponse;
      timestamp: string;
    }
  | {
      id: string;
      role: "agent";
      kind: "message";
      text: string;
      timestamp: string;
    }
  | {
      id: string;
      role: "system";
      kind: "status" | "error";
      text: string;
      timestamp: string;
    };

// ── ChatBubble ────────────────────────────────────────────────────────────

const CLARIFICATION_PREFIX = "[Clarification answers]";

export function ChatBubble({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    if (message.text.startsWith(CLARIFICATION_PREFIX)) {
      const lines = message.text
        .slice(CLARIFICATION_PREFIX.length)
        .trim()
        .split("\n")
        .filter((l) => l.trim());
      return (
        <div className="flex items-start gap-2 justify-end">
          <div className="max-w-[85%] rounded-2xl rounded-tr-sm border border-border-light bg-surface-alt px-4 py-3">
            <div className="flex flex-col gap-1.5">
              {lines.map((line, i) => {
                const colon = line.indexOf(":");
                const value = colon !== -1 ? line.slice(colon + 1).trim() : line.trim();
                return (
                  <div
                    key={i}
                    className="flex items-center gap-2 text-sm text-text-primary"
                  >
                    <span className="icon-[mdi--check-circle] shrink-0 text-sm text-success" />
                    <span>{value}</span>
                  </div>
                );
              })}
            </div>
          </div>
          <Avatar className="h-7 w-7 shrink-0">
            <AvatarFallback className="bg-accent/20 text-accent">
              <span className="icon-[mdi--account] text-sm" />
            </AvatarFallback>
          </Avatar>
        </div>
      );
    }

    return (
      <div className="flex items-start gap-2 justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-tr-sm bg-accent px-4 py-2.5 text-sm text-white">
          {message.text}
        </div>
        <Avatar className="h-7 w-7 shrink-0">
          <AvatarFallback className="bg-accent/20 text-accent">
            <span className="icon-[mdi--account] text-sm" />
          </AvatarFallback>
        </Avatar>
      </div>
    );
  }

  if (message.role === "system") {
    return (
      <div className="flex justify-center py-1">
        <span
          className={cn(
            "flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium",
            message.kind === "error"
              ? "bg-error-light text-error"
              : "bg-surface-alt text-text-muted",
          )}
        >
          <span className="icon-[mdi--information-outline] text-xs" />
          {message.text}
        </span>
      </div>
    );
  }

  // Plain agent text (delta / summary — rarely used today)
  if (message.kind === "message") {
    return (
      <div className="flex w-full items-start gap-2">
        <Avatar className="h-7 w-7 shrink-0">
          <AvatarFallback className="bg-surface-alt">
            <span className="icon-[mdi--robot-outline] text-sm text-text-muted" />
          </AvatarFallback>
        </Avatar>
        <div className="min-w-0 flex-1 pt-1 whitespace-pre-wrap break-words text-sm">
          {message.text}
        </div>
      </div>
    );
  }

  // ── Agent event item — unified flat layout ───────────────────────────
  //
  // Every item kind uses the same three-column structure:
  //   [Icon w-7]  [Content min-w-0 flex-1]  [MetaStatus w-20]

  const { item } = message;
  const visual = KIND_VISUALS[item.kind] ?? KIND_VISUALS["codex:other"];
  const title = itemTitle(item);
  const meta = <MetaStatus status={item.status} time={formatTime(item.updated_at)} />;

  // agent_message: prose body + meta (no title label).  Skip if empty.
  if (item.kind === "codex:agent_message") {
    const hasText = readString(item.payload_jsonb.text) !== null;
    if (!hasText) return null;
    return (
      <div className="flex w-full items-start gap-2">
        <Avatar className="h-7 w-7 shrink-0">
          <AvatarFallback className="bg-surface-alt">
            <span className={cn(visual.icon, visual.iconColor, "text-sm")} />
          </AvatarFallback>
        </Avatar>
        <div className="min-w-0 flex-1">
          <AgentMessageBody payload={item.payload_jsonb} />
        </div>
        <div className="pt-[6px]">{meta}</div>
      </div>
    );
  }

  // Reasoning with empty content: ultra-compact single row
  const reasoningContent =
    readString(item.payload_jsonb.summary) ?? readString(item.payload_jsonb.content);
  if (item.kind === "codex:reasoning" && reasoningContent === null) {
    return (
      <div className="flex w-full items-center gap-2">
        <Avatar className="h-7 w-7 shrink-0">
          <AvatarFallback className="bg-surface-alt">
            <span className={cn(visual.icon, visual.iconColor, "text-sm")} />
          </AvatarFallback>
        </Avatar>
        <span className="min-w-0 flex-1 truncate text-xs text-text-muted">
          {i18n.t("items.reasoning")}
        </span>
        {meta}
      </div>
    );
  }

  const isCompact = COMPACT_KINDS.has(item.kind);
  const detailText = isCompact ? compactDetailText(item) : null;

  // Compact kinds (command / tool / internal): title + inline ▸ + meta
  if (isCompact) {
    return (
      <div className="flex w-full items-start gap-2">
        <Avatar className="h-7 w-7 shrink-0">
          <AvatarFallback className="bg-surface-alt">
            <span className={cn(visual.icon, visual.iconColor, "text-sm")} />
          </AvatarFallback>
        </Avatar>
        <div className="min-w-0 flex-1 pt-1">
          {detailText !== null ? (
            <details className="group">
              <summary className="flex cursor-pointer select-none items-center gap-2">
                <span className="min-w-0 truncate text-[13px] font-medium text-text-primary">
                  {title}
                </span>
                <span className="icon-[mdi--chevron-right] shrink-0 text-xs text-text-muted transition-transform group-open:rotate-90" />
              </summary>
              <pre className="mt-1.5 max-h-64 overflow-auto rounded-md border border-border-light bg-surface-alt px-2 py-1.5 font-mono text-[11px] leading-[1.45] text-text-primary">
                {detailText}
              </pre>
            </details>
          ) : (
            <span className="truncate text-[13px] font-medium text-text-primary">
              {title}
            </span>
          )}
        </div>
        <div className="pt-[6px]">{meta}</div>
      </div>
    );
  }

  // Non-compact kinds (plan, reasoning w/ content, file_change, error, etc.)
  return (
    <div className="flex w-full items-start gap-2">
      <Avatar className="h-7 w-7 shrink-0">
        <AvatarFallback className="bg-surface-alt">
          <span className={cn(visual.icon, visual.iconColor, "text-sm")} />
        </AvatarFallback>
      </Avatar>
      <div className="min-w-0 flex-1 pt-1">
        <div className="flex items-center gap-2">
          <span
            className="min-w-0 flex-1 truncate text-[13px] font-medium text-text-primary"
            title={title}
          >
            {title}
          </span>
          {meta}
        </div>
        {renderItemBody(item)}
      </div>
    </div>
  );
}
