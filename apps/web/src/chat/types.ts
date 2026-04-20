/** Chat-surface helpers and aliases pulled out of App.tsx. */

import type {
  EventResponse,
  SessionResponse,
  SessionStatus,
} from "@polaris/shared-types";

import type { ChatMessage } from "../ChatBubble";

export const TERMINAL_STATUSES: SessionStatus[] = ["completed", "failed", "interrupted"];
export const SESSIONS_PAGE_SIZE = 3;
export const WORKSPACE_CONTAINER_PATH = "/workspace";

export type SessionWithItems = {
  session: SessionResponse;
  items: EventResponse[];
};

// ── Small helpers ─────────────────────────────────────────────────────────

export function nowIso(): string {
  return new Date().toISOString();
}

/** UUID v4.  Falls back to a manual implementation for older browsers /
 *  test environments that don't expose crypto.randomUUID. */
export function msgId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
}

/** Theia IDE URL from workspace.ide_url — used as iframe src directly. */
export function resolveIdeUrl(ideUrl: string | null | undefined): string | null {
  if (ideUrl !== undefined && ideUrl !== null && ideUrl.trim().length > 0) {
    return ideUrl.trim();
  }
  return null;
}

/** Flatten SessionWithItems[] into the chat bubble sequence: for each
 *  session, emit the user message, then its items in order, then any
 *  terminal system bubble (error / interrupted). */
export function buildMessages(sessions: SessionWithItems[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  for (const { session, items } of sessions) {
    messages.push({
      id: `user-${session.id}`,
      role: "user",
      kind: "text",
      text: session.user_message,
      timestamp: session.created_at,
    });
    for (const item of items) {
      messages.push({
        id: `item-${item.id}`,
        role: "agent",
        kind: "item",
        item,
        timestamp: item.updated_at,
      });
    }
    if (session.status === "failed" && session.error_message !== null) {
      messages.push({
        id: `err-${session.id}`,
        role: "system",
        kind: "error",
        text: session.error_message,
        timestamp: session.finished_at ?? nowIso(),
      });
    } else if (session.status === "interrupted") {
      messages.push({
        id: `int-${session.id}`,
        role: "system",
        kind: "status",
        text: "Turn interrupted",
        timestamp: session.finished_at ?? nowIso(),
      });
    }
  }
  return messages;
}
