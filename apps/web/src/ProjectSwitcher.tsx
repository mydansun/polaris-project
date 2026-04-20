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
};

export function ProjectSwitcher({
  open,
  onOpenChange,
  projects,
  activeProjectId,
  onSelect,
  onCreate,
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
          {ordered.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-text-muted">
              {t("projects.empty")}
            </div>
          ) : (
            <ul className="flex flex-col py-2">
              {ordered.map((p) => {
                const isActive = p.id === activeProjectId;
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
                        <span className="min-w-0 flex-1 truncate text-sm font-medium">
                          {p.name}
                        </span>
                        <span className="shrink-0 text-[10px] tabular-nums text-text-muted">
                          {formatUpdated(p.updated_at)}
                        </span>
                      </div>
                      {p.description !== null && p.description.length > 0 ? (
                        <div className="line-clamp-1 text-[11px] text-text-muted">
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
