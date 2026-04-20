import { useTranslation } from "react-i18next";
import type { SessionStats } from "../App";

/** Two-column activity bar sitting under the chat input.
 *
 *  Left column = ``file_change_count`` (from the worker's inotify
 *  watcher under the session's project_root).
 *  Right column = ``playwright_call_count`` (from codex:mcp_tool_call
 *  completions whose payload.server is "playwright").
 *
 *  Two coalescing layers feed this component:
 *    1. Worker-side 500ms debounce — a burst of fs/playwright events
 *       becomes one SSE frame per window.
 *    2. Client-side ~400ms leading-edge-with-trailing-flush throttle
 *       (in ``useSessionEventHandler``) — low-frequency bursts that
 *       arrive as separate single-delta frames get summed into one
 *       "+N" animation rather than a chain of "+1" flashes.
 *  We drive the "+N" float + main-number pulse off ``flashKey`` —
 *  bumped on every coalesced flush — so the CSS keyframe restarts even
 *  when back-to-back flushes arrive.
 */
export function StatusBar({ stats }: { stats: SessionStats }) {
  const { t } = useTranslation();
  return (
    <div className="border-t border-border-light bg-surface-subtle text-[12px] text-text-muted">
      {/* Inner wrapper: capped + left-aligned so the counters don't
          stretch into a wasteland when the chat column is wide. */}
      <div className="flex w-full max-w-md items-stretch">
        <Cell
          icon="icon-[mdi--file-edit-outline]"
          label={t("statusBar.fileChanges")}
          count={stats.fileChanges}
          delta={stats.fileDelta}
          flashKey={stats.flashKey}
        />
        <div className="w-px bg-border-light" />
        <Cell
          icon="icon-[mdi--play-network-outline]"
          label={t("statusBar.testCalls")}
          count={stats.testCalls}
          delta={stats.testDelta}
          flashKey={stats.flashKey}
        />
      </div>
    </div>
  );
}

function Cell({
  icon,
  label,
  count,
  delta,
  flashKey,
}: {
  icon: string;
  label: string;
  count: number;
  delta: number;
  flashKey: number;
}) {
  const shouldFlash = delta > 0;
  return (
    <div className="flex flex-1 items-center gap-2 px-4 py-2">
      <span className={`${icon} text-sm shrink-0`} />
      <span className="truncate">{label}</span>
      <span className="relative ml-auto inline-flex items-center">
        <span
          // `flashKey` in the key forces React to unmount/mount on each
          // SSE frame so the CSS animation restarts.
          key={`count-${flashKey}`}
          className={
            "tabular-nums text-text-primary font-medium" +
            (shouldFlash ? " animate-count-pulse" : "")
          }
        >
          {count}
        </span>
        {shouldFlash && (
          <span
            key={`delta-${flashKey}`}
            className="pointer-events-none absolute -top-1 right-0 select-none text-[11px] font-semibold text-accent animate-count-float"
          >
            +{delta}
          </span>
        )}
      </span>
    </div>
  );
}
