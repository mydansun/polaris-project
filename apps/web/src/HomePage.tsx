/**
 * Personal home — list of the user's projects with publish-status
 * badges.  Polled every 10s while mounted so a freshly-finished build
 * surfaces without a manual refresh.
 *
 * Empty state ⇒ navigate to /projects/new (the existing welcome page).
 *
 * Visual direction: "Quiet Operator".  Inter for human content;
 * JetBrains Mono only for technical metadata (status pills, domain
 * hash, timestamps).  Reuses existing Card / Badge / cn from
 * @polaris/ui — no new design tokens.  See the design notes for the
 * 4-row card density rule and why the project ordinal lives top-right.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { ProjectResponse, UserResponse } from "@polaris/shared-types";
import { Avatar, AvatarFallback, Button, Card, cn } from "@polaris/ui";
import { useTranslation } from "react-i18next";

import { listProjects, logout as apiLogout } from "./api";

const REFRESH_INTERVAL_MS = 10_000;


type DeploymentSummary = NonNullable<ProjectResponse["latest_deployment"]>;
type DeploymentStatus = DeploymentSummary["status"];


export function HomePage({
  user,
  onLogout,
}: {
  user: UserResponse;
  onLogout: () => void;
}) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [projects, setProjects] = useState<ProjectResponse[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Fetch + 10-second poll.  Cancels in-flight on unmount or interval
  // re-trigger via the ``alive`` flag.
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;

    async function tick() {
      try {
        const list = await listProjects();
        if (alive) {
          setProjects(list);
          setError(null);
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : "Failed to load projects");
      } finally {
        if (alive) timer = window.setTimeout(tick, REFRESH_INTERVAL_MS);
      }
    }
    tick();
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  // Empty state → redirect to the welcome / new-project page.  Driven
  // here so refreshing on / always lands somewhere meaningful.
  useEffect(() => {
    if (projects !== null && projects.length === 0) {
      navigate("/projects/new", { replace: true });
    }
  }, [projects, navigate]);

  const handleLogout = useCallback(async () => {
    try {
      await apiLogout();
    } finally {
      onLogout();
      navigate("/", { replace: true });
    }
  }, [navigate, onLogout]);

  if (projects === null) {
    return (
      <main className="flex h-dvh items-center justify-center bg-surface">
        <img src="/polaris.svg" alt="" className="h-10 w-10 animate-pulse" />
      </main>
    );
  }

  return (
    <main className="min-h-dvh bg-surface text-text-primary">
      <header className="flex items-center justify-between border-b border-border-light px-8 py-4">
        <div className="flex items-center gap-2">
          <img src="/polaris.svg" alt="" className="h-6 w-6" />
          <h1 className="text-base font-semibold">Polaris</h1>
        </div>
        <div className="flex items-center gap-3">
          <Button
            size="sm"
            onClick={() => navigate("/projects/new")}
            className="text-sm"
          >
            <span className="icon-[lucide--plus] -ml-1" />
            {t("home.newProject", "New project")}
          </Button>
          <Avatar className="h-8 w-8 cursor-pointer" onClick={handleLogout} title={user.email}>
            <AvatarFallback className="bg-accent text-white text-xs">
              {(user.name?.[0] ?? user.email[0] ?? "?").toUpperCase()}
            </AvatarFallback>
          </Avatar>
        </div>
      </header>

      <section className="mx-auto max-w-6xl px-8 py-10">
        <div className="mb-6 flex items-baseline justify-between">
          <h2 className="text-xl font-semibold tracking-tight">
            {t("home.title", "Projects")}
          </h2>
          <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-text-muted">
            {projects.length} {t("home.activeCount", "active")}
          </span>
        </div>

        {error !== null && (
          <div className="mb-4 rounded border border-error bg-error-light px-3 py-2 text-xs text-error">
            {error}
          </div>
        )}

        <ol
          className="grid gap-6"
          style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}
        >
          {projects.map((p, idx) => (
            <ProjectCardLi key={p.id} index={idx} project={p} t={t} />
          ))}
        </ol>
      </section>
    </main>
  );
}


function ProjectCardLi({
  index,
  project,
  t,
}: {
  index: number;
  project: ProjectResponse;
  t: ReturnType<typeof useTranslation>["t"];
}) {
  const ld = project.latest_deployment;
  const status: DeploymentStatus | "draft" = ld?.status ?? "draft";
  const isPublishing = status === "queued" || status === "building";
  const updated = formatRelative(project.updated_at, t);

  return (
    <li
      data-testid="project-card"
      className="enter-stagger"
      style={{ ["--stagger-i" as string]: index }}
    >
      <Link
        to={`/projects/${project.id}`}
        className="block h-full no-underline focus:outline-none"
      >
      <Card
        className={cn(
          "group relative h-full cursor-pointer overflow-hidden p-4 transition-colors hover:border-text-primary",
          isPublishing && "running-border",
        )}
      >
        {/* Card preview: prefer the post-publish website screenshot
            (filled by the publish pipeline / one-shot seed backfill).
            Fall back to the inspiration mood-board only when no real
            screenshot exists.  When neither exists yet (a draft
            project, or a project whose discovery turn crashed before
            the mood board was generated), render a neutral placeholder
            so every card has the same visual height — drafts mixed
            with live cards otherwise look uneven and "broken". */}
        {(() => {
          const shot = project.latest_deployment?.screenshot_url ?? null;
          const moodboard = project.mood_board_url ?? null;
          const url = shot ?? moodboard;
          if (url) {
            return (
              <img
                src={url}
                alt=""
                loading="lazy"
                className="mb-3 aspect-[3/2] w-full rounded border border-border-light object-cover object-top opacity-95 group-hover:opacity-100"
              />
            );
          }
          return (
            <div
              data-testid="project-card-placeholder"
              className="mb-3 flex aspect-[3/2] w-full items-center justify-center rounded border border-dashed border-border-light bg-surface-alt text-text-muted/40"
            >
              <span className="icon-[lucide--image] text-[28px]" aria-hidden />
            </div>
          );
        })()}
        <h3 className="truncate pr-6 text-sm font-semibold leading-tight">
          {project.name}
        </h3>
        <p className="mt-1 line-clamp-2 min-h-[2.4em] text-xs text-text-muted">
          {project.description?.trim() || (
            <span className="text-text-muted/60">—</span>
          )}
        </p>
        <hr className="my-3 border-border-light" />
        <StatusLine ld={ld} sessionStatus={project.latest_session_status} t={t} />
        <p className="mt-1 font-mono text-[10px] text-text-muted">{updated}</p>
      </Card>
      </Link>
    </li>
  );
}


function StatusLine({
  ld,
  sessionStatus,
  t,
}: {
  ld: ProjectResponse["latest_deployment"];
  sessionStatus: ProjectResponse["latest_session_status"];
  t: ReturnType<typeof useTranslation>["t"];
}) {
  // Deployment status takes precedence; otherwise fall back to the
  // most recent agent session so projects whose codex / discovery turn
  // crashed pre-publish surface as "Failed" instead of silently "Draft".
  const status: DeploymentStatus | "draft" = (() => {
    if (ld?.status) return ld.status;
    if (sessionStatus === "failed") return "failed";
    if (sessionStatus === "queued" || sessionStatus === "running") return "building";
    return "draft";
  })();
  // Tone-pair per status:
  //   - bg / text both pulled from the same hue family so the pill
  //     reads as a single chip (light tint background + saturated
  //     text), not a colored dot floating in muted text.
  //   - Tokens (--color-success / --color-error / --color-accent /
  //     --color-text-muted) come from app.css — no new colors added.
  const tone = (() => {
    switch (status) {
      case "ready":
        return {
          bg: "bg-success-light",
          text: "text-success",
          dot: "bg-success",
        };
      case "queued":
      case "building":
        return {
          bg: "bg-accent/10",
          text: "text-accent",
          dot: "bg-accent animate-pulse",
        };
      case "failed":
        return {
          bg: "bg-error-light",
          text: "text-error",
          dot: "bg-error",
        };
      case "rolled_back":
        return {
          bg: "bg-text-muted/10",
          text: "text-text-muted",
          dot: "bg-text-muted",
        };
      default:
        return {
          bg: "bg-text-muted/10",
          text: "text-text-muted",
          dot: "bg-text-muted",
        };
    }
  })();
  const label = (() => {
    switch (status) {
      case "ready": return t("home.status.live", "Live");
      case "queued":
      case "building": return t("home.status.publishing", "Publishing");
      case "failed": return t("home.status.failed", "Failed");
      case "rolled_back": return t("home.status.rolledBack", "Rolled back");
      default: return t("home.status.draft", "Draft");
    }
  })();

  return (
    <div className="flex items-center gap-2">
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium leading-none",
          tone.bg,
          tone.text,
        )}
      >
        <span className={cn("h-1.5 w-1.5 rounded-full", tone.dot)} />
        {label}
      </span>
      {ld && status === "ready" && ld.domain && (
        <ProdUrlButton url={`https://${ld.domain}`} domain={ld.domain} />
      )}
    </div>
  );
}


function ProdUrlButton({ url, domain }: { url: string; domain: string }) {
  return (
    <button
      type="button"
      data-prod-url={url}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        window.open(url, "_blank", "noopener,noreferrer");
      }}
      className="ml-auto inline-flex items-center gap-1 truncate font-mono text-[10px] text-text-muted hover:text-accent hover:underline"
      title={domain}
    >
      <span className="icon-[lucide--external-link] text-[10px]" />
      {shortHash(domain)}
    </button>
  );
}


// Show the leading hash chunk of "<uuid>.prod.<domain>" rather than the
// full string.  Title attribute carries the full domain for hover.
function shortHash(domain: string): string {
  const first = domain.split(".")[0];
  if (!first) return domain;
  if (first.length <= 8) return first;
  return `${first.slice(0, 8)}…`;
}


function formatRelative(iso: string, t: ReturnType<typeof useTranslation>["t"]): string {
  const then = new Date(iso).getTime();
  const sec = Math.round((Date.now() - then) / 1000);
  if (sec < 60) return t("projects.justNow");
  if (sec < 3600) return t("projects.minutesAgo", { count: Math.round(sec / 60) });
  if (sec < 86400) return t("projects.hoursAgo", { count: Math.round(sec / 3600) });
  if (sec < 7 * 86400) return t("projects.daysAgo", { count: Math.round(sec / 86400) });
  return new Date(iso).toLocaleDateString();
}
