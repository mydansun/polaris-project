import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Badge,
  Button,
  ScrollArea,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@polaris/ui";
import type { DeploymentResponse, DeploymentStatus } from "@polaris/shared-types";
import {
  listDeployments,
  rollbackDeployment,
  subscribeDeploymentEvents,
} from "./api";

type PublishPanelProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  /** True while an agent session is queued or running for this project.
   *  When true, the Publish button is disabled (can't queue a second
   *  session on top of an active one). */
  agentBusy: boolean;
  /** Called when the user clicks "Publish now".  App-side fires
   *  `handleSendMessage(i18n.t("publish.requestMessage"))`. */
  onRequestPublish: () => void;
};

type StreamEvent =
  | { type: "log"; channel: "build" | "smoke"; data: string }
  | { type: "status"; status: DeploymentStatus }
  | { type: "ready"; domain: string; image: string }
  | { type: "failed"; error: string };

const STATUS_KEYS: Record<DeploymentStatus, string> = {
  queued: "publish.statuses.queued",
  building: "publish.statuses.building",
  deploying: "publish.statuses.deploying",
  ready: "publish.statuses.ready",
  failed: "publish.statuses.failed",
  rolled_back: "publish.statuses.rolledBack",
};

const STATUS_VARIANT: Record<DeploymentStatus, "default" | "secondary" | "destructive" | "outline"> = {
  queued: "secondary",
  building: "secondary",
  deploying: "secondary",
  ready: "default",
  failed: "destructive",
  rolled_back: "outline",
};

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function PublishPanel({
  open,
  onOpenChange,
  projectId,
  agentBusy,
  onRequestPublish,
}: PublishPanelProps) {
  const { t } = useTranslation();
  const [deployments, setDeployments] = useState<DeploymentResponse[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [liveLog, setLiveLog] = useState<string>("");
  const [liveStatus, setLiveStatus] = useState<DeploymentStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sseRef = useRef<EventSource | null>(null);

  const refresh = useCallback(async () => {
    try {
      const rows = await listDeployments(projectId, 20);
      setDeployments(rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("publish.errors.loadFailed"));
    }
  }, [projectId, t]);

  useEffect(() => {
    if (!open) return;
    setError(null);
    refresh();
  }, [open, projectId, refresh]);

  useEffect(() => {
    if (!open) return;
    if (activeId !== null) return;
    const inFlight = deployments.find((d) =>
      ["queued", "building", "deploying"].includes(d.status),
    );
    if (inFlight) {
      setActiveId(inFlight.id);
      setLiveStatus(inFlight.status);
    }
  }, [open, deployments, activeId]);

  useEffect(() => {
    if (activeId === null) return;
    setLiveLog("");
    const src = subscribeDeploymentEvents(
      activeId,
      (raw) => {
        const ev = raw as StreamEvent;
        if (ev.type === "log") {
          setLiveLog((prev) => prev + ev.data);
        } else if (ev.type === "status") {
          setLiveStatus(ev.status);
        } else if (ev.type === "ready") {
          setLiveStatus("ready");
          refresh();
        } else if (ev.type === "failed") {
          setLiveStatus("failed");
          setError(ev.error || t("publish.errors.publishFailed"));
          refresh();
        }
      },
      () => {},
    );
    sseRef.current = src;
    return () => {
      src.close();
      sseRef.current = null;
    };
  }, [activeId, refresh, t]);

  const handleRollback = useCallback(
    async (commit: string | null) => {
      if (!commit) return;
      if (!window.confirm(t("publish.rollbackConfirm", { commit: commit.slice(0, 7) }))) {
        return;
      }
      try {
        const dep = await rollbackDeployment(projectId, commit);
        setActiveId(dep.id);
        setLiveStatus(dep.status);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : t("publish.errors.rollbackFailed"));
      }
    },
    [projectId, refresh, t],
  );

  const current = deployments.find((d) => d.status === "ready") ?? null;
  const inFlight = liveStatus !== null && liveStatus !== "ready" && liveStatus !== "failed";

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 sm:max-w-lg">
        <SheetHeader className="border-b border-border-light px-5 py-4">
          <SheetTitle>{t("publish.title")}</SheetTitle>
          <SheetDescription>{t("publish.description")}</SheetDescription>
        </SheetHeader>

        <div className="flex flex-col gap-4 border-b border-border-light px-5 py-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p className="text-xs uppercase text-text-muted">{t("publish.current")}</p>
              {current ? (
                <a
                  href={`https://${current.domain}`}
                  target="_blank"
                  rel="noreferrer"
                  className="truncate text-sm font-medium text-accent hover:underline"
                >
                  {current.domain}
                </a>
              ) : (
                <p className="text-sm text-text-muted">{t("publish.neverPublished")}</p>
              )}
            </div>
            {inFlight ? (
              <Badge variant={STATUS_VARIANT[liveStatus!] ?? "secondary"}>
                {t(STATUS_KEYS[liveStatus!] ?? "publish.statuses.queued")}
              </Badge>
            ) : null}
          </div>

          {!inFlight ? (
            <div className="flex flex-col gap-2">
              <Button
                onClick={() => {
                  onRequestPublish();
                  onOpenChange(false);
                }}
                disabled={agentBusy}
                className="self-start"
              >
                {t("publish.requestButton")}
              </Button>
              {agentBusy ? (
                <p className="flex items-center gap-1.5 text-xs text-text-muted">
                  <span className="icon-[mdi--clock-outline] text-sm" />
                  {t("publish.agentBusy")}
                </p>
              ) : null}
            </div>
          ) : null}

          {error !== null ? (
            <p className="rounded border border-error/30 bg-error-light px-3 py-2 text-sm text-error">
              {error}
            </p>
          ) : null}

          {liveLog !== "" ? (
            <pre className="max-h-56 overflow-auto rounded bg-slate-950 p-3 font-mono text-[11px] leading-snug text-slate-100">
              {liveLog}
            </pre>
          ) : null}
        </div>

        <ScrollArea className="flex-1 px-5 py-3">
          <div className="flex flex-col gap-2">
            <p className="text-xs uppercase text-text-muted">{t("publish.history")}</p>
            {deployments.length === 0 ? (
              <p className="text-sm text-text-muted">{t("publish.noDeployments")}</p>
            ) : (
              deployments.map((d) => {
                const commit = (d.git_commit_hash || "").slice(0, 7) || "—";
                const canRollback =
                  d.status === "ready" &&
                  d.id !== current?.id &&
                  d.git_commit_hash !== null;
                return (
                  <div
                    key={d.id}
                    className="flex items-center justify-between rounded border border-border-light px-3 py-2"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <Badge variant={STATUS_VARIANT[d.status]}>
                          {t(STATUS_KEYS[d.status])}
                        </Badge>
                        <span className="font-mono text-xs">{commit}</span>
                      </div>
                      <p className="mt-1 text-[11px] text-text-muted">
                        {formatTime(d.created_at)}
                      </p>
                      {d.error !== null ? (
                        <p className="mt-1 line-clamp-2 text-[11px] text-error">{d.error}</p>
                      ) : null}
                    </div>
                    {canRollback ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleRollback(d.git_commit_hash)}
                        className="text-xs"
                      >
                        rollback
                      </Button>
                    ) : null}
                  </div>
                );
              })
            )}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}
