/** Chat-surface helpers and aliases pulled out of App.tsx. */

import type {
  ClarificationRecord,
  EventResponse,
  SessionResponse,
  SessionStatus,
} from "@polaris/shared-types";

import type { ChatMessage } from "../ChatBubble";
import i18n from "../i18n";

export const TERMINAL_STATUSES: SessionStatus[] = ["completed", "failed", "interrupted"];
export const SESSIONS_PAGE_SIZE = 3;
export const WORKSPACE_CONTAINER_PATH = "/workspace";

export type SessionWithItems = {
  session: SessionResponse;
  items: EventResponse[];
  clarifications: ClarificationRecord[];
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

/** A user-side steer message — additional input the user typed while
 *  Codex was already running on a session.  We hold these client-side
 *  (the backend forwards them straight to codex over WS without
 *  storing them as ``codex:*`` items the way it does for normal turn
 *  output), so they have to be kept in component state and merged in
 *  here for rendering.  ``id`` is locally-minted; ``timestamp``
 *  controls intercalation between agent items. */
export type SteeredMessage = {
  id: string;
  text: string;
  timestamp: string;
};

/** Flatten SessionWithItems[] into the chat bubble sequence: for each
 *  session, emit the initial user message, then steered messages +
 *  agent items in chronological order, then any terminal system
 *  bubble (error / interrupted). */
export function buildMessages(
  sessions: SessionWithItems[],
  steersBySessionId: Record<string, SteeredMessage[]> = {},
): ChatMessage[] {
  const messages: ChatMessage[] = [];
  for (const { session, items, clarifications } of sessions) {
    messages.push({
      id: `user-${session.id}`,
      role: "user",
      kind: "text",
      text: session.user_message,
      timestamp: session.created_at,
    });
    // Merge steered user messages with agent items by timestamp so the
    // chat reads chronologically.  Steers tagged role=user, items
    // role=agent.
    const steers = steersBySessionId[session.id] ?? [];
    type Inter = { ts: string; msg: ChatMessage };
    const interleaved: Inter[] = [];
    for (const item of items) {
      interleaved.push({
        ts: item.updated_at,
        msg: {
          id: `item-${item.id}`,
          role: "agent",
          kind: "item",
          item,
          timestamp: item.updated_at,
        },
      });
    }
    for (const steer of steers) {
      interleaved.push({
        ts: steer.timestamp,
        msg: {
          id: `steer-${steer.id}`,
          role: "user",
          kind: "text",
          text: steer.text,
          timestamp: steer.timestamp,
        },
      });
    }
    for (const clar of clarifications) {
      const ts = clar.answered_at ?? clar.created_at;
      interleaved.push({
        ts,
        msg: {
          id: `clar-${clar.id}`,
          role: "user",
          kind: "clarification",
          questions: clar.questions,
          answers: clar.answers,
          timestamp: ts,
        },
      });
    }
    interleaved.sort((a, b) => a.ts.localeCompare(b.ts));
    for (const { msg } of interleaved) messages.push(msg);

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
        // Resolved via the i18n singleton (not useTranslation) so
        // buildMessages stays a plain function and callers don't have
        // to thread the translator through.  Re-renders driven by an
        // i18n language change won't re-run buildMessages — but
        // language switches in this app trigger a state-bump that
        // rebuilds the project shell anyway.
        text: i18n.t("app.turnInterrupted"),
        timestamp: session.finished_at ?? nowIso(),
      });
    }
  }
  return messages;
}
