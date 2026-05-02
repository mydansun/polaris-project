/**
 * Per-event-kind visual metadata (icon + title + compact-mode rules).
 *
 * Split out of `ChatBubble.tsx` so these static tables live near the
 * `EventKind` enum and can be imported by both the main bubble and the
 * noise-cluster renderer without dragging in React.
 */

import i18n from "../i18n";
import type { EventKind, EventResponse } from "@polaris/shared-types";

// ── Per-kind icons ────────────────────────────────────────────────────────

export type KindVisual = {
  icon: string;
  /** Tailwind color utility for the icon — differentiates kinds at a glance. */
  iconColor: string;
};

export const KIND_VISUALS: Record<EventKind, KindVisual> = {
  "codex:agent_message":      { icon: "icon-[mdi--message-outline]",                 iconColor: "text-accent" },
  "codex:plan":               { icon: "icon-[mdi--clipboard-text-outline]",          iconColor: "text-amber-500" },
  "codex:reasoning":          { icon: "icon-[mdi--brain]",                           iconColor: "text-violet-500" },
  "codex:command_execution":  { icon: "icon-[mdi--console-line]",                    iconColor: "text-emerald-500" },
  "codex:file_change":        { icon: "icon-[mdi--file-document-edit-outline]",      iconColor: "text-sky-500" },
  "codex:mcp_tool_call":      { icon: "icon-[mdi--tools]",                           iconColor: "text-fuchsia-500" },
  "codex:dynamic_tool_call":  { icon: "icon-[mdi--cog-outline]",                     iconColor: "text-fuchsia-500" },
  "codex:web_search":         { icon: "icon-[mdi--web]",                             iconColor: "text-cyan-500" },
  "codex:error":              { icon: "icon-[mdi--alert-circle-outline]",            iconColor: "text-error" },
  "codex:other":              { icon: "icon-[mdi--shape-outline]",                   iconColor: "text-text-muted" },
  "discovery:clarifying":     { icon: "icon-[mdi--compass-outline]",                 iconColor: "text-rose-500" },
  "discovery:references":     { icon: "icon-[mdi--image-search-outline]",            iconColor: "text-rose-500" },
  "discovery:compiled":       { icon: "icon-[mdi--clipboard-check-outline]",         iconColor: "text-rose-500" },
  "discovery:moodboard":      { icon: "icon-[mdi--palette-outline]",                 iconColor: "text-rose-500" },
};

/**
 * Kinds that render as a single compact row (title + expand caret + status).
 * Detail content appears below only on expand — cuts per-card height from
 * ~60px to ~32px for the common collapsed state.
 */
export const COMPACT_KINDS = new Set<EventKind>([
  "codex:command_execution",
  "codex:mcp_tool_call",
  "codex:dynamic_tool_call",
]);

// ── Small payload helpers ─────────────────────────────────────────────────

export function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

export function readNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

export function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is string => typeof v === "string" && v.length > 0);
}

export function commandSummary(command: unknown): string | null {
  if (typeof command === "string") return command;
  if (Array.isArray(command)) {
    return command.filter((v) => typeof v === "string").join(" ");
  }
  return null;
}

export function basename(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash === -1 ? path : path.slice(slash + 1);
}

// ── Titles ────────────────────────────────────────────────────────────────

export function itemTitle(item: EventResponse): string {
  const payload = item.payload_jsonb;
  const t = i18n.t.bind(i18n);
  if (item.kind === "codex:agent_message") return t("items.agentMessage");
  if (item.kind === "codex:plan")           return t("items.planning");
  if (item.kind === "codex:reasoning")      return t("items.reasoning");
  if (item.kind === "codex:command_execution") return t("items.executeCommand");
  if (item.kind === "codex:file_change") {
    const paths = readStringArray(payload.paths);
    if (paths.length === 0) return t("items.fileChange");
    if (paths.length === 1) return basename(paths[0]);
    return `${basename(paths[0])} +${paths.length - 1}`;
  }
  if (item.kind === "codex:mcp_tool_call")      return t("items.toolCall");
  if (item.kind === "codex:dynamic_tool_call")  return t("items.internalCall");
  if (item.kind === "codex:web_search")         return readString(payload.query) ?? t("items.webSearch");
  if (item.kind === "codex:error")              return readString(payload.message) ?? t("items.error");
  if (item.kind === "discovery:clarifying")     return t("items.clarifying");
  if (item.kind === "discovery:references")     return t("items.references");
  if (item.kind === "discovery:compiled")       return t("items.compiled");
  if (item.kind === "discovery:moodboard")      return t("items.moodBoard");
  return t("items.step");
}

// ── Compact-row inline details (command output + tool args) ───────────────

export function compactDetailText(item: EventResponse): string | null {
  const p = item.payload_jsonb;
  if (item.kind === "codex:command_execution") {
    const parts: string[] = [];
    const cmd = commandSummary(p.command);
    if (cmd !== null) parts.push(`$ ${cmd}`);
    const exit = readNumber(p.exit_code);
    if (typeof exit === "number") parts.push(`\nexit ${exit}`);
    const output = readString(p.output);
    if (output !== null) parts.push("\n" + output);
    return parts.join("\n").trim() || null;
  }
  if (item.kind === "codex:mcp_tool_call" || item.kind === "codex:dynamic_tool_call") {
    const parts: string[] = [];
    const server = readString(p.server);
    const tool = readString(p.tool);
    const label = [server, tool].filter((v): v is string => v !== null).join(" / ");
    if (label) parts.push(label);
    const args = p.arguments;
    if (args !== undefined && args !== null) {
      parts.push(typeof args === "string" ? args : JSON.stringify(args, null, 2));
    }
    return parts.join("\n").trim() || null;
  }
  return null;
}
