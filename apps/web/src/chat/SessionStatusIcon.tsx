import type { SessionStatus } from "@polaris/shared-types";

export type SessionUiStatus = SessionStatus | "idle";

/**
 * Header session-status pill as a semantic icon.  Hover tooltip carries
 * the full status word.  Running + queued share the pulsing dot so motion
 * reads as "something is happening".
 */
export function SessionStatusIcon({ status }: { status: SessionUiStatus }) {
  const base = "flex h-6 w-6 items-center justify-center rounded-full";
  if (status === "running" || status === "queued") {
    return (
      <div
        className={`${base} bg-accent/10`}
        title={status === "running" ? "Running" : "Queued"}
        aria-label={status}
      >
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
        </span>
      </div>
    );
  }
  if (status === "completed") {
    return (
      <div
        className={`${base} bg-success-light`}
        title="Completed"
        aria-label="completed"
      >
        <span className="icon-[mdi--check-circle] text-base text-success" />
      </div>
    );
  }
  if (status === "failed") {
    return (
      <div className={`${base} bg-error-light`} title="Failed" aria-label="failed">
        <span className="icon-[mdi--alert-circle] text-base text-error" />
      </div>
    );
  }
  if (status === "interrupted") {
    return (
      <div
        className={`${base} bg-amber-50`}
        title="Interrupted"
        aria-label="interrupted"
      >
        <span className="icon-[mdi--stop-circle] text-base text-amber-500" />
      </div>
    );
  }
  // idle — blank placeholder keeps the header layout stable.
  return (
    <div className={`${base} text-text-muted`} title="Idle" aria-label="idle">
      <span className="icon-[mdi--circle-outline] text-base" />
    </div>
  );
}
