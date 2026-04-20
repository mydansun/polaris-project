/**
 * Fixed-width meta column (status dot + HH:MM:SS) used by every card variant.
 * Split out so the status indicator can be imported by other surfaces (noise
 * cluster, clarification card, etc.) without re-entering ChatBubble.
 */

import type { EventStatus } from "@polaris/shared-types";

export function formatTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

/** `w-20 shrink-0` keeps status+time aligned vertically across all card kinds. */
export function MetaStatus({
  status,
  time,
}: {
  status: EventStatus;
  time: string;
}) {
  return (
    <div className="flex w-20 shrink-0 items-center justify-end gap-1.5 text-[11px] text-text-muted">
      <StatusDot status={status} />
      <span className="tabular-nums">{time}</span>
    </div>
  );
}

export function StatusDot({ status }: { status: EventStatus }) {
  if (status === "completed") {
    return (
      <span
        className="icon-[mdi--check-circle] text-success text-sm"
        aria-label="completed"
      />
    );
  }
  if (status === "failed") {
    return (
      <span
        className="icon-[mdi--alert-circle] text-error text-sm"
        aria-label="failed"
      />
    );
  }
  return (
    <span className="relative flex h-2 w-2" aria-label="in progress">
      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
      <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
    </span>
  );
}
