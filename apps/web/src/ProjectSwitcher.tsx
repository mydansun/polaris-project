import {
  Button,
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  cn,
} from "@polaris/ui";
import { useTranslation } from "react-i18next";
import type { ProjectResponse } from "@polaris/shared-types";

type ProjectSwitcherProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projects: ProjectResponse[];
  activeProjectId: string | null;
  onSelect: (projectId: string) => void;
  onCreate: () => void;
  onHome: () => void;
};

type StatusKind = "live" | "publishing" | "failed" | "draft";

/** Map a project into one of four UI buckets.
 *
 * Deployment status takes precedence (a project that's actually live or
 * publishing is the most informative signal), but when there's no
 * deployment row we fall back to the latest agent session — that's how
 * we catch projects whose codex / discovery turn crashed before any
 * publish was attempted (otherwise they'd render as gray drafts despite
 * clearly being broken).
 */
function projectStatus(p: ProjectResponse): StatusKind {
  const dep = p.latest_deployment?.status;
  if (dep === "ready") return "live";
  if (dep === "queued" || dep === "building") return "publishing";
  if (dep === "failed") return "failed";
  if (dep === undefined || dep === null) {
    const sess = p.latest_session_status;
    if (sess === "failed") return "failed";
    if (sess === "queued" || sess === "running") return "publishing";
  }
  return "draft";
}

function StatusDot({ kind }: { kind: StatusKind }) {
  // Tokens: --color-success / --color-accent / --color-error / --color-text-muted.
  if (kind === "failed") {
    return (
      <span
        className="icon-[lucide--alert-circle] shrink-0 text-[14px] text-error"
        aria-label="failed"
      />
    );
  }
  const dotColor =
    kind === "live"
      ? "bg-success"
      : kind === "publishing"
        ? "bg-accent animate-pulse"
        : "bg-text-muted/40";
  return (
    <span
      className={cn("h-2 w-2 shrink-0 rounded-full", dotColor)}
      aria-label={kind}
    />
  );
}

export function ProjectSwitcher({
  open,
  onOpenChange,
  projects,
  activeProjectId,
  onSelect,
  onCreate,
  onHome,
}: ProjectSwitcherProps) {
  const { t } = useTranslation();

  function formatUpdated(iso: string): string {
    const date = new Date(iso);
    const now = new Date();
    const secAgo = Math.round((now.getTime() - date.getTime()) / 1000);
    if (secAgo < 60) return t("projects.justNow");
    if (secAgo < 3600) return t("projects.minutesAgo", { count: Math.round(secAgo / 60) });
    if (secAgo < 86400) return t("projects.hoursAgo", { count: Math.round(secAgo / 3600) });
    if (secAgo < 7 * 86400) return t("projects.daysAgo", { count: Math.round(secAgo / 86400) });
    return date.toLocaleDateString();
  }

  const ordered = [...projects].sort((a, b) =>
    b.updated_at.localeCompare(a.updated_at),
  );

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="left" className="w-80 gap-0 p-0">
        <SheetHeader className="flex-row items-center justify-between gap-2 p-4">
          <SheetTitle>{t("projects.title")}</SheetTitle>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              onCreate();
              onOpenChange(false);
            }}
            className="h-8"
          >
            <span className="icon-[mdi--plus] text-base" />
            <span className="ml-1">{t("projects.new")}</span>
          </Button>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <button
            type="button"
            onClick={() => {
              onHome();
              onOpenChange(false);
            }}
            className="flex w-full cursor-pointer items-center gap-2 border-l-2 border-transparent px-4 py-2.5 text-left text-sm font-medium transition-colors hover:bg-surface-alt"
          >
            <span className="icon-[lucide--home] text-base text-text-muted" />
            <span>{t("projects.home")}</span>
          </button>
          <hr className="border-border-light" />
          {ordered.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-text-muted">
              {t("projects.empty")}
            </div>
          ) : (
            <ul className="flex flex-col py-2">
              {ordered.map((p) => {
                const isActive = p.id === activeProjectId;
                const kind = projectStatus(p);
                return (
                  <li key={p.id}>
                    <button
                      type="button"
                      onClick={() => {
                        if (isActive) {
                          onOpenChange(false);
                          return;
                        }
                        onSelect(p.id);
                        onOpenChange(false);
                      }}
                      className={cn(
                        "flex w-full cursor-pointer flex-col items-stretch gap-0.5 border-l-2 px-4 py-2.5 text-left transition-colors",
                        isActive
                          ? "border-accent bg-accent/10 text-text-primary"
                          : "border-transparent hover:bg-surface-alt",
                      )}
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <StatusDot kind={kind} />
                        <span className="min-w-0 flex-1 truncate text-sm font-medium">
                          {p.name}
                        </span>
                        <span className="shrink-0 text-[10px] tabular-nums text-text-muted">
                          {formatUpdated(p.updated_at)}
                        </span>
                      </div>
                      {p.description !== null && p.description.length > 0 ? (
                        <div className="line-clamp-1 pl-4 text-[11px] text-text-muted">
                          {p.description}
                        </div>
                      ) : null}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
