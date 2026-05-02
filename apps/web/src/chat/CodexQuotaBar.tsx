/**
 * CodexQuotaBar — small percentage indicator next to the chat user avatar.
 *
 * Shows the codex 5-hour rate-limit window (the same number `/status` would
 * print).  Polls `GET /projects/{id}/codex-quota` every 60s; the API caches
 * the upstream WS query for 30s so the load is negligible.
 *
 * Hidden until a usable reading lands — no flicker on cold workspaces or
 * when codex hasn't seen a prompt yet.  Color shifts to amber > 75% and red
 * > 95% so the user notices before they hit the wall.
 */
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getProjectCodexQuota,
  type CodexQuotaResponse,
  type CodexQuotaWindow,
} from "../api";

const POLL_INTERVAL_MS = 60_000;

function barColor(pct: number): string {
  if (pct >= 95) return "bg-error";
  if (pct >= 75) return "bg-amber-500";
  return "bg-accent";
}

function formatResetsIn(resetsAt: number | null): string | null {
  if (!resetsAt) return null;
  const deltaMs = resetsAt * 1000 - Date.now();
  if (deltaMs <= 0) return null;
  const minutes = Math.round(deltaMs / 60_000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const mm = minutes % 60;
  if (hours < 24) return mm ? `${hours}h${mm}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const hh = hours % 24;
  return hh ? `${days}d${hh}h` : `${days}d`;
}

type Props = {
  projectId: string | null;
};

export function CodexQuotaBar({ projectId }: Props) {
  const { t } = useTranslation();
  const [quota, setQuota] = useState<CodexQuotaResponse | null>(null);

  useEffect(() => {
    if (projectId === null) {
      setQuota(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await getProjectCodexQuota(projectId);
        if (!cancelled) setQuota(next);
      } catch {
        // Network blips just defer to the next tick — keep last reading.
      }
    };
    tick();
    const handle = window.setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, [projectId]);

  const primary: CodexQuotaWindow | null =
    quota && quota.available ? quota.primary : null;

  const tooltip = useMemo(() => {
    if (!primary) return "";
    const pct = Math.round(primary.used_percent);
    const resetIn = formatResetsIn(primary.resets_at);
    if (resetIn) {
      return t("chat.codexQuota.tooltipWithReset", { percent: pct, reset: resetIn });
    }
    return t("chat.codexQuota.tooltip", { percent: pct });
  }, [primary, t]);

  if (!primary) return null;

  const pct = Math.max(0, Math.min(100, primary.used_percent));
  const display = Math.round(pct);

  return (
    <div
      className="flex items-center gap-1.5 text-xs text-text-muted"
      title={tooltip}
      aria-label={tooltip}
    >
      <div className="h-1.5 w-12 overflow-hidden rounded-full bg-border-light/60">
        <div
          className={`h-full ${barColor(pct)} transition-[width] duration-500 ease-out`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="tabular-nums">{display}%</span>
    </div>
  );
}
