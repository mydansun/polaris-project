/**
 * Grouping rules for the chat stream.
 *
 * Noise items (command_execution, empty reasoning, mcp_tool_call) that sit
 * between two meaningful items are collected into a single collapsible
 * cluster.  Meaningful items always render standalone.
 *
 * Pure utility — no React.
 */

import i18n from "../i18n";
import type { EventKind } from "@polaris/shared-types";

import type { ChatMessage } from "../ChatBubble";
import { readString } from "./itemVisuals";

/** Event kinds collapsed into a cluster when adjacent. */
export const NOISE_KINDS = new Set<EventKind>([
  "codex:command_execution",
  "codex:reasoning",
  "codex:mcp_tool_call",
]);

export function isNoise(msg: ChatMessage): boolean {
  if (msg.role !== "agent" || msg.kind !== "item") return false;
  if (!NOISE_KINDS.has(msg.item.kind)) return false;
  // reasoning WITH visible text is meaningful — don't collapse.
  if (msg.item.kind === "codex:reasoning") {
    const s =
      readString(msg.item.payload_jsonb.summary) ??
      readString(msg.item.payload_jsonb.content);
    if (s !== null) return false;
  }
  return true;
}

export type MessageGroup =
  | { type: "single"; message: ChatMessage }
  | { type: "cluster"; messages: ChatMessage[] };

export function groupMessages(messages: ChatMessage[]): MessageGroup[] {
  const groups: MessageGroup[] = [];
  let noiseBuffer: ChatMessage[] = [];

  function flushNoise() {
    if (noiseBuffer.length === 0) return;
    if (noiseBuffer.length === 1) {
      groups.push({ type: "single", message: noiseBuffer[0] });
    } else {
      groups.push({ type: "cluster", messages: [...noiseBuffer] });
    }
    noiseBuffer = [];
  }

  for (const msg of messages) {
    if (isNoise(msg)) {
      noiseBuffer.push(msg);
    } else {
      flushNoise();
      groups.push({ type: "single", message: msg });
    }
  }
  flushNoise();
  return groups;
}

/** Label for a noise-kind chip inside a NoiseCluster summary row. */
export function kindLabel(kind: EventKind): string {
  const labels: Partial<Record<EventKind, string>> = {
    "codex:command_execution": i18n.t("items.executeCommand"),
    "codex:reasoning": i18n.t("items.reasoning"),
    "codex:mcp_tool_call": i18n.t("items.toolCall"),
  };
  return labels[kind] ?? kind;
}

/** Count noise items per kind, preserving first-seen order. */
export function countByKind(
  messages: ChatMessage[],
): { kind: EventKind; count: number }[] {
  const order: EventKind[] = [];
  const counts = new Map<EventKind, number>();
  for (const msg of messages) {
    if (msg.role !== "agent" || msg.kind !== "item") continue;
    const k = msg.item.kind;
    if (!counts.has(k)) order.push(k);
    counts.set(k, (counts.get(k) ?? 0) + 1);
  }
  return order.map((k) => ({ kind: k, count: counts.get(k)! }));
}
