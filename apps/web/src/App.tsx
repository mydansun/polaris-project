import { useCallback, useEffect, useRef, useState } from "react";
import type {
  BrowserSessionResponse,
  ClarificationRequest,
  ClarificationResponse,
  ProjectDetailResponse,
  ProjectResponse,
  ReadyResponse,
  SessionResponse,
  SessionStatus,
  UserResponse,
} from "@polaris/shared-types";
import {
  createProject,
  createSession,
  ensureBrowserSession,
  ensureWorkspaceIdeSession,
  ensureWorkspaceRuntime,
  restartWorkspaceRuntime,
  getBrowserSession,
  getMe,
  getPendingClarification,
  getProject,
  getReady,
  getSession,
  getWorkspaceRuntime,
  interruptSession,
  listProjectSessions,
  listProjects,
  logout as apiLogout,
  QuotaError,
  stopBrowserSession,
  submitClarification,
  subscribeSessionEvents,
} from "./api";
import { ChatPane } from "./ChatPane";
import { PublishPanel } from "./PublishPanel";
// CreateProjectDialog removed — new projects are created implicitly
// when the user sends the first message with project === null (same flow
// as a brand-new user with no projects).
import { EditorPane } from "./EditorPane";
import { BrowserPane } from "./BrowserPane";
import { LoginPage } from "./LoginPage";
import { ProjectSwitcher } from "./ProjectSwitcher";
import { QuotaDialog } from "./QuotaDialog";
import {
  TERMINAL_STATUSES,
  SESSIONS_PAGE_SIZE,
  WORKSPACE_CONTAINER_PATH,
  buildMessages,
  resolveIdeUrl,
  type SessionWithItems,
} from "./chat/types";
import i18n from "./i18n";
import { useMcpOverlay } from "./hooks/useMcpOverlay";
import { useRuntimeUrlPoller } from "./hooks/useRuntimeUrlPoller";
import { useSessionEventHandler } from "./hooks/useSessionEventHandler";
import { useSplitPane } from "./hooks/useSplitPane";

export type PaneMode = "inline" | "hidden";
export type RightPaneTab = "browser" | "ide" | "none";

/** Status-bar counters displayed under the chat input.  `fileDelta` /
 * `testDelta` drive the "+N" float animation; `flashKey` is bumped on
 * every SSE frame so the CSS keyframe re-runs even when the previous
 * one hasn't finished. */
export type SessionStats = {
  fileChanges: number;
  testCalls: number;
  fileDelta: number;
  testDelta: number;
  flashKey: number;
};

const EMPTY_SESSION_STATS: SessionStats = {
  fileChanges: 0,
  testCalls: 0,
  fileDelta: 0,
  testDelta: 0,
  flashKey: 0,
};

export function App() {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [project, setProject] = useState<ProjectDetailResponse | null>(null);
  const [sessions, setSessions] = useState<SessionWithItems[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [sessionStats, setSessionStats] = useState<SessionStats>(EMPTY_SESSION_STATS);
  // Pagination: true while there are older sessions to fetch.
  const [hasMoreSessions, setHasMoreSessions] = useState(false);
  const [isLoadingOlderSessions, setIsLoadingOlderSessions] = useState(false);
  const [browserSession, setBrowserSession] = useState<BrowserSessionResponse | null>(null);
  const [ready, setReady] = useState<ReadyResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isOpeningBrowser, setIsOpeningBrowser] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [quotaError, setQuotaError] = useState<QuotaError | null>(null);
  // Project list for the switcher drawer.  Loaded on bootstrap; refreshed
  // whenever we create a new project or open the switcher.
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  // Flag the agent-message-delta stream so ChatPane can switch its
  // auto-scroll from smooth (nice for discrete new items) to auto
  // (instant; avoids visible jitter while tokens arrive 20x/sec).
  const [isStreamingAgentMsg, setIsStreamingAgentMsg] = useState(false);
  const [clarificationRequest, setClarificationRequest] = useState<ClarificationRequest | null>(null);
  // Session + run id that own the currently-shown clarification card.  We
  // thread both back into submitClarification so the API routes answers to
  // the exact AgentRun that asked — no status='running' guessing.
  const [clarificationSessionId, setClarificationSessionId] = useState<string | null>(null);
  const [clarificationRunId, setClarificationRunId] = useState<string | null>(null);
  const [pendingPlanApproval, setPendingPlanApproval] = useState(false);
  // Right pane: "browser", "ide", or "none" (hidden).  Starts hidden.
  // Auto-switches to "browser" when browser session is ready, then to
  // "ide" when set_project_root fires.
  const [rightPane, setRightPane] = useState<RightPaneTab>("none");
  const autoRevealedBrowserForProjectRef = useRef<string | null>(null);
  const autoRevealedEditorForProjectRef = useRef<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const ideRequestProjectIdRef = useRef<string | null>(null);
  // Incremented on every handleSelectProject call; async loaders capture
  // the value at entry and bail if the user switched to a different
  // project before they finished.  Prevents races when users rapid-click.
  const loadGenerationRef = useRef<number>(0);

  // Resizable split pane — state + drag handler live in a dedicated hook.
  const { splitPct, dragPct, dragging, containerRef, startDrag } = useSplitPane();

  const activeSession = activeSessionId !== null
    ? sessions.find((entry) => entry.session.id === activeSessionId)?.session ?? null
    : null;
  const sessionStatus: SessionStatus | "idle" = activeSession?.status ?? "idle";
  const sessionInFlight = sessionStatus === "queued" || sessionStatus === "running";

  // MCP tool call overlay: true when a Playwright (or any MCP) tool call is
  // actively running.  Only blocks the browser iframe during those narrow
  // windows, not during the entire session.
  const mcpToolCallActive = activeSessionId !== null && sessions.some((entry) =>
    entry.session.id === activeSessionId &&
    entry.items.some(
      (item) => item.kind === "codex:mcp_tool_call" && item.status === "started",
    ),
  );

  // Overlay debounced so it doesn't flicker across rapid consecutive calls
  // (navigate → click → type → screenshot).  See hook for the 400ms edge.
  const mcpOverlayVisible = useMcpOverlay(mcpToolCallActive);

  // Fallback 30s poller for runtime URLs while project_root is null.
  // Primary path is the project_root_changed SSE (handled by the main
  // project-load useEffect below); this catches edge cases where SSE is
  // unreliable (no active session, transient disconnect, etc.).
  useRuntimeUrlPoller(project, setProject);

  const projectRoot = project?.workspace?.project_root ?? null;
  // Folder the IDE iframe should open.  Skeleton-guarded upstream so this
  // is only read when we've decided to mount the iframe.
  const ideFolder = projectRoot ?? WORKSPACE_CONTAINER_PATH;
  const ideUrl = resolveIdeUrl(project?.workspace?.ide_url);
  const messages = buildMessages(sessions);

  // Seed StatusBar from the active session's persisted counts whenever it
  // changes (new session picked / page refresh during running session /
  // switched project).  Deltas reset to 0 so no "+N" flash fires on
  // hydration — only live SSE frames trigger the animation.
  useEffect(() => {
    if (activeSessionId === null) {
      setSessionStats(EMPTY_SESSION_STATS);
      return;
    }
    const entry = sessions.find((e) => e.session.id === activeSessionId);
    if (entry === undefined) return;
    const s = entry.session;
    setSessionStats((prev) => ({
      fileChanges: s.file_change_count ?? 0,
      testCalls: s.playwright_call_count ?? 0,
      fileDelta: 0,
      testDelta: 0,
      flashKey: prev.flashKey, // no flash on hydration
    }));
    // Only re-seed on activeSessionId changes; SSE already owns live updates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId]);

  const applySessionEvent = useSessionEventHandler({
    setSessions,
    setProject,
    setClarificationRequest,
    setClarificationSessionId,
    setClarificationRunId,
    setIsStreamingAgentMsg,
    setPendingPlanApproval,
    setRightPane,
    setSessionStats,
    onSessionTerminal: () => {
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
    },
  });

  /** Load project detail + session history, subscribe SSE if latest
   *  session is still running.  Generation-guarded so a mid-flight switch
   *  wins. */
  const loadProject = useCallback(
    async (projectId: string, generation: number): Promise<void> => {
      const detail = await getProject(projectId);
      if (loadGenerationRef.current !== generation) return;
      setProject(detail);

      let sessionEntries: SessionWithItems[] = [];
      try {
        const projectSessions = await listProjectSessions(projectId, { limit: SESSIONS_PAGE_SIZE });
        if (loadGenerationRef.current !== generation) return;
        setHasMoreSessions(projectSessions.length >= SESSIONS_PAGE_SIZE);
        if (projectSessions.length > 0) {
          const detailed = await Promise.all(
            projectSessions.map((s) => getSession(s.id)),
          );
          if (loadGenerationRef.current !== generation) return;
          sessionEntries = detailed.map((d) => {
            const { runs, ...rest } = d;
            const items = runs.flatMap((r) => r.events);
            return { session: rest as SessionResponse, items };
          });
        }
      } catch {
        /* soft-fail: empty chat history is a fine fallback */
      }

      setSessions(sessionEntries);
      const latest = sessionEntries[sessionEntries.length - 1];
      setActiveSessionId(latest?.session.id ?? null);

      // Recover pending clarification card from the clarifications table.
      setClarificationRequest(null);
      try {
        const { pending } = await getPendingClarification(projectId);
        if (loadGenerationRef.current !== generation) return;
        if (pending) setClarificationRequest(pending);
      } catch {
        /* soft-fail: no pending card is a fine fallback */
      }

      if (latest && !TERMINAL_STATUSES.includes(latest.session.status)) {
        eventSourceRef.current?.close();
        eventSourceRef.current = subscribeSessionEvents(
          latest.session.id,
          applySessionEvent,
          () => {},
        );
      }

      // Browser session (if any) — best-effort, no session dependency.
      try {
        const session = await getBrowserSession(projectId);
        if (loadGenerationRef.current !== generation) return;
        setBrowserSession(session);
      } catch {
        if (loadGenerationRef.current === generation) {
          setBrowserSession(null);
        }
      }
    },
    // applySessionEvent is a stable closure over state setters (hoisted
    // function), so we intentionally leave it out of the dep list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  /** Load older sessions when the user scrolls to the top of the chat. */
  const loadOlderSessions = useCallback(async () => {
    if (!project || isLoadingOlderSessions || !hasMoreSessions) return;
    const oldest = sessions[0];
    if (!oldest) return;
    setIsLoadingOlderSessions(true);
    try {
      const olderSessions = await listProjectSessions(project.id, {
        limit: SESSIONS_PAGE_SIZE,
        beforeSequence: oldest.session.sequence,
      });
      setHasMoreSessions(olderSessions.length >= SESSIONS_PAGE_SIZE);
      if (olderSessions.length > 0) {
        const detailed = await Promise.all(
          olderSessions.map((s) => getSession(s.id)),
        );
        const olderEntries: SessionWithItems[] = detailed.map((d) => {
          const { runs, ...rest } = d;
          const items = runs.flatMap((r) => r.events);
          return { session: rest as SessionResponse, items };
        });
        setSessions((prev) => [...olderEntries, ...prev]);
      }
    } catch {
      /* soft-fail */
    } finally {
      setIsLoadingOlderSessions(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id, sessions, isLoadingOlderSessions, hasMoreSessions]);

  /** Swap to another project: tear down current SSE + pane state, call
   *  the idempotent runtime ensure (this is what reanimates a workspace
   *  whose container was killed out-of-band), then repopulate sessions. */
  const handleSelectProject = useCallback(
    async (projectId: string): Promise<void> => {
      const generation = ++loadGenerationRef.current;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;

      setError(null);
      setSessions([]);
      setActiveSessionId(null);
      setBrowserSession(null);
      setRightPane("none");
      autoRevealedEditorForProjectRef.current = null;
      autoRevealedBrowserForProjectRef.current = null;

      try {
        await ensureWorkspaceRuntime(projectId);
      } catch (err) {
        if (loadGenerationRef.current !== generation) return;
        setError(err instanceof Error ? err.message : "Unable to start workspace");
        // Still try to load project metadata so the user sees the name
        // + can retry the runtime request manually.
      }

      if (loadGenerationRef.current !== generation) return;
      try {
        await loadProject(projectId, generation);
      } catch (err) {
        if (loadGenerationRef.current !== generation) return;
        setError(err instanceof Error ? err.message : "Failed to load project");
      }
    },
    [loadProject],
  );

  /** Fetch-and-store the full project list for the switcher. */
  const refreshProjects = useCallback(async (): Promise<ProjectResponse[]> => {
    const list = await listProjects();
    setProjects(list);
    return list;
  }, []);

  useEffect(() => {
    let alive = true;
    async function bootstrapSession() {
      try {
        const authUser = await getMe();
        if (!alive) return;
        setUser(authUser);

        try {
          const list = await refreshProjects();
          if (!alive || list.length === 0) return;
          // Reuse the switch path — it also reconciles the per-project
          // runtime, so page refresh auto-revives a workspace whose
          // containers were killed out of band (make clear, reboot…).
          await handleSelectProject(list[0].id);
        } catch (restoreError) {
          if (alive) {
            setError(
              restoreError instanceof Error
                ? restoreError.message
                : "Unable to restore recent project",
            );
          }
        }
      } catch {
        /* not logged in */
      } finally {
        if (alive) setAuthChecked(true);
      }
    }

    bootstrapSession();
    getReady()
      .then((response) => {
        if (alive) setReady(response);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : "API is not ready");
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (project === null) {
      setBrowserSession(null);
      return;
    }
    let alive = true;
    // Re-fetches on project_root changes as well — the project_root_changed
    // SSE event updates `project.workspace.project_root` in state, which
    // flows through this dep and re-triggers the URL pull.  Before the
    // agent declares project_root, the API returns null ide_url / 404
    // browser_session; after, the real URLs come back.
    getBrowserSession(project.id)
      .then((session) => {
        if (alive) setBrowserSession(session);
      })
      .catch(() => {
        if (alive) setBrowserSession(null);
      });
    getWorkspaceRuntime(project.id)
      .then((runtime) => {
        if (!alive || project.workspace === null) return;
        setProject((current) => {
          if (current === null || current.id !== project.id || current.workspace === null) {
            return current;
          }
          return {
            ...current,
            workspace: {
              ...current.workspace,
              ide_url: runtime.ide_url,
              ide_status: runtime.status,
            },
          };
        });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [project?.id, project?.workspace?.project_root]);

  useEffect(() => {
    return () => {
      eventSourceRef.current?.close();
    };
  }, []);

  useEffect(() => {
    if (project === null || project.workspace === null) {
      ideRequestProjectIdRef.current = null;
      return;
    }
    // Hold off on ensureWorkspaceIdeSession until the agent has declared
    // project_root — the API returns 409 before that, and surfacing a
    // "Unable to configure IDE" error for an expected/transient state
    // is a bad UX.  The project_root_changed SSE will re-run this effect
    // once the signal lands.
    if (project.workspace.project_root === null) {
      ideRequestProjectIdRef.current = null;
      return;
    }
    if (
      project.workspace.ide_url !== null
      || project.workspace.ide_status === "ready"
      || project.workspace.ide_status === "starting"
    ) {
      ideRequestProjectIdRef.current = null;
      return;
    }
    if (ideRequestProjectIdRef.current === project.id) return;

    ideRequestProjectIdRef.current = project.id;
    ensureWorkspaceIdeSession(project.id)
      .then((session) => {
        setProject((current) => {
          if (current === null || current.id !== project.id || current.workspace === null) {
            return current;
          }
          return {
            ...current,
            workspace: {
              ...current.workspace,
              ide_url: session.ide_url,
              ide_status: session.ide_status,
            },
          };
        });
        getBrowserSession(project.id)
          .then((s) => setBrowserSession(s))
          .catch(() => {});
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : "Unable to configure code editor");
      });
  }, [project?.id, project?.workspace?.project_root, project?.workspace?.ide_url, project?.workspace?.ide_status]);

  // Auto-reveal right pane ONLY when the agent declares a project root.
  // Until that signal arrives, the IDE iframe has no folder to open and
  // the browser iframe has no dev server to show, so both stay hidden
  // behind the "awaiting agent" placeholder.
  //
  // Once triggered per project, the user's hide button sticks: we don't
  // re-open even if the signal stays true on re-renders.  We pick IDE
  // (not browser) as the default reveal target since Codex's scaffolding
  // has just finished writing code — that's the most relevant surface.
  useEffect(() => {
    const pid = project?.id ?? null;
    const root = project?.workspace?.project_root ?? null;
    if (pid === null || root === null) return;
    if (autoRevealedEditorForProjectRef.current === pid) return;
    autoRevealedEditorForProjectRef.current = pid;
    setRightPane("ide");
  }, [project?.id, project?.workspace?.project_root]);

  function handleNewProject() {
    // Reset to the "no project" state — same as a brand-new user.
    // The next message in handleSendMessage will auto-create a project
    // using the message text as the project name.
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
    setProject(null);
    setSessions([]);
    setActiveSessionId(null);
    setBrowserSession(null);
    setRightPane("none");
    autoRevealedEditorForProjectRef.current = null;
    autoRevealedBrowserForProjectRef.current = null;
    setError(null);
  }

  async function handleSendMessage(text: string) {
    setPendingPlanApproval(false);
    setIsSubmitting(true);
    setError(null);
    eventSourceRef.current?.close();

    try {
      let currentProject = project;

      if (currentProject === null) {
        const projectName = text.length > 60 ? text.slice(0, 57) + "..." : text;
        currentProject = await createProject({
          name: projectName,
          description: text,
        });
        setProject(currentProject);
        setRightPane("none");
        autoRevealedEditorForProjectRef.current = null;
        autoRevealedBrowserForProjectRef.current = null;
        refreshProjects().catch(() => {});
        // Fire-and-forget: start the workspace container concurrently with
        // the session creation.  Worker's _resolve_container_ip waits up
        // to 30s for the container, so parallel startup stays within that
        // budget.
        ensureWorkspaceRuntime(currentProject.id).catch(() => {});
      }

      // The first message of any project always routes through discovery
      // (we need a design brief before Codex starts) and lands Codex in
      // plan mode so the user can review the plan before execution.
      // Every subsequent message skips the plan round and writes code
      // directly — after the initial plan-approve cycle the user is just
      // iterating, another plan-proceed handshake on every turn is
      // friction.  The discover/re-discover button is hidden on the
      // welcome screen (see `onDiscover` prop below).
      const isFirstMessage = sessions.length === 0;
      const newSession = await createSession(currentProject.id, {
        message: text,
        mode: isFirstMessage ? "discover_then_build" : "build_direct",
      });
      setSessions((prev) => [...prev, { session: newSession, items: [] }]);
      setActiveSessionId(newSession.id);

      eventSourceRef.current = subscribeSessionEvents(newSession.id, applySessionEvent, () => {});
    } catch (err) {
      if (err instanceof QuotaError) {
        setQuotaError(err);
      } else {
        setError(err instanceof Error ? err.message : "Unable to start turn");
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleDiscover(text: string) {
    // Identical to handleSendMessage but sends mode="discover" so the worker
    // runs the design-intent pre-agent (LangGraph clarifier + Pinterest +
    // multimodal compiler) before handing the compiled brief off to Codex.
    if (!text) return;
    if (!ready) {
      setError("API is not ready");
      return;
    }
    setIsSubmitting(true);
    setError(null);
    eventSourceRef.current?.close();

    try {
      let currentProject = project;
      if (currentProject === null) {
        const projectName = text.length > 60 ? text.slice(0, 57) + "..." : text;
        currentProject = await createProject({ name: projectName, description: text });
        setProject(currentProject);
        setRightPane("none");
        autoRevealedEditorForProjectRef.current = null;
        autoRevealedBrowserForProjectRef.current = null;
        refreshProjects().catch(() => {});
        ensureWorkspaceRuntime(currentProject.id).catch(() => {});
      }

      const newSession = await createSession(currentProject.id, {
        message: text,
        mode: "discover_then_build",
      });
      setSessions((prev) => [...prev, { session: newSession, items: [] }]);
      setActiveSessionId(newSession.id);

      eventSourceRef.current = subscribeSessionEvents(newSession.id, applySessionEvent, () => {});
    } catch (err) {
      if (err instanceof QuotaError) {
        setQuotaError(err);
      } else {
        setError(err instanceof Error ? err.message : "Unable to start turn");
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleInterrupt() {
    if (activeSessionId === null) return;
    try {
      const updated = await interruptSession(activeSessionId);
      // Optimistic local flip so the header / stop button reflect
      // "interrupted" immediately, even if the SSE `session_completed`
      // frame from the API is a hair behind.  `useSessionEventHandler`
      // does the same merge when the SSE frame lands — idempotent.
      setSessions((prev) =>
        prev.map((entry) =>
          entry.session.id === updated.id
            ? { ...entry, session: updated }
            : entry,
        ),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to interrupt");
    }
  }

  async function handleProceedWithPlan() {
    setPendingPlanApproval(false);
    if (project === null) return;
    setIsSubmitting(true);
    setError(null);
    eventSourceRef.current?.close();
    try {
      const newSession = await createSession(project.id, {
        // Localized — "按计划继续。" / "Proceed with the plan." / etc.
        // Using the i18n singleton (not useTranslation) keeps this handler
        // free of hook plumbing; same pattern as chat/messageGrouping.ts.
        message: i18n.t("app.proceedWithPlan"),
        mode: "build_direct",
      });
      setSessions((prev) => [...prev, { session: newSession, items: [] }]);
      setActiveSessionId(newSession.id);
      eventSourceRef.current = subscribeSessionEvents(newSession.id, applySessionEvent, () => {});
    } catch (err) {
      if (err instanceof QuotaError) {
        setQuotaError(err);
      } else {
        setError(err instanceof Error ? err.message : "Unable to proceed");
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleClarificationSubmit(response: ClarificationResponse) {
    if (project === null) return;
    try {
      await submitClarification(project.id, {
        ...response,
        session_id: response.session_id ?? clarificationSessionId ?? undefined,
        run_id: response.run_id ?? clarificationRunId ?? undefined,
      });
      setClarificationRequest(null);
      setClarificationSessionId(null);
      setClarificationRunId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to submit clarification");
    }
  }

  async function handleRestartWorkspace() {
    if (project === null) return;
    setError(null);
    const pid = project.id;
    // Flip ide_status to skeleton while containers restart.
    setProject((current) =>
      current === null || current.workspace === null || current.id !== pid
        ? current
        : {
            ...current,
            workspace: { ...current.workspace, ide_status: "starting" },
          },
    );
    setBrowserSession(null);
    try {
      await restartWorkspaceRuntime(pid);
      // Poll until the runtime is actually ready (containers healthy,
      // IDE and browser URLs responding).  Without this, the iframes
      // load against containers that aren't up yet → 404.
      for (let attempt = 0; attempt < 30; attempt++) {
        await new Promise((r) => setTimeout(r, 2000));
        if (loadGenerationRef.current !== loadGenerationRef.current) break;
        try {
          const runtime = await getWorkspaceRuntime(pid);
          if (runtime.status === "ready" && runtime.ide_url) {
            setProject((current) => {
              if (current === null || current.workspace === null || current.id !== pid) return current;
              return {
                ...current,
                workspace: {
                  ...current.workspace,
                  ide_url: runtime.ide_url,
                  ide_status: runtime.status,
                },
              };
            });
            try {
              const session = await getBrowserSession(pid);
              setBrowserSession(session);
            } catch {
              setBrowserSession(null);
            }
            break;
          }
        } catch {
          // Runtime not ready yet — keep polling.
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to restart workspace");
    }
  }

  async function handleOpenBrowser() {
    if (project === null || project.workspace === null) return;
    setIsOpeningBrowser(true);
    setError(null);
    try {
      const session = await ensureBrowserSession(project.id);
      setBrowserSession(session);
      setProject({
        ...project,
        workspace: { ...project.workspace, current_browser_session_id: session.id },
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to open browser");
    } finally {
      setIsOpeningBrowser(false);
    }
  }

  async function handleLogout() {
    try {
      await apiLogout();
    } catch {
      /* ignore */
    }
    setUser(null);
    setProject(null);
    setSessions([]);
    setActiveSessionId(null);
    setBrowserSession(null);
  }

  async function handleStopBrowser() {
    if (project === null || project.workspace === null || browserSession === null) return;
    setError(null);
    try {
      const session = await stopBrowserSession(project.id);
      setBrowserSession(session);
      setProject({
        ...project,
        workspace: { ...project.workspace, current_browser_session_id: null },
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to stop browser");
    }
  }

  if (!authChecked) {
    return (
      <div className="flex h-dvh items-center justify-center bg-surface">
        <img src="/polaris.svg" alt="" className="h-10 w-10 animate-pulse" />
      </div>
    );
  }

  if (user === null) {
    return <LoginPage />;
  }

  const finalMessage = activeSession?.final_message ?? null;

  const handleDividerDown = startDrag;

  const showRight = rightPane !== "none";

  return (
    <main ref={containerRef} className="relative flex h-dvh overflow-hidden bg-border">
      {/* Left: Chat */}
      <div style={{ width: showRight ? `${splitPct}%` : "100%", minWidth: 280 }} className="h-full flex-shrink-0 overflow-hidden">
      <ChatPane
        messages={messages}
        project={project}
        sessionStatus={sessionStatus}
        isSubmitting={isSubmitting}
        error={error}
        ready={ready !== null}
        user={user}
        onSendMessage={handleSendMessage}
        // Discover/re-discover button temporarily hidden — first message is
        // always auto-routed through discovery by handleSendMessage (see
        // `sessions.length === 0` branch), and we don't expose re-discovery
        // mid-project yet.
        onDiscover={undefined}
        onNewProject={handleNewProject}
        onOpenSwitcher={() => {
          // Refresh the list lazily when the user opens the drawer so
          // new/deleted projects show up without needing a page reload.
          refreshProjects().catch(() => {});
          setSwitcherOpen(true);
        }}
        onLogout={handleLogout}
        onInterrupt={handleInterrupt}
        onRestartWorkspace={handleRestartWorkspace}
        onOpenPublish={() => setPublishOpen(true)}
        isStreamingAgentMsg={isStreamingAgentMsg}
        rightPane={rightPane}
        onRightPaneChange={setRightPane}
        hasMoreSessions={hasMoreSessions}
        isLoadingOlderSessions={isLoadingOlderSessions}
        onLoadOlderSessions={loadOlderSessions}
        clarificationRequest={clarificationRequest}
        onClarificationSubmit={handleClarificationSubmit}
        pendingPlanApproval={pendingPlanApproval}
        onProceedWithPlan={handleProceedWithPlan}
        sessionStats={sessionStats}
      />
      </div>

      {/* Draggable divider */}
      {showRight && (
        <div
          className="w-2 flex-shrink-0 cursor-col-resize bg-border hover:bg-accent/30 active:bg-accent/50 transition-colors"
          onMouseDown={handleDividerDown}
          title="Drag to resize"
        />
      )}

      {/* Right: IDE or Browser */}
      {showRight && (
        <div className="flex-1 min-w-[280px] overflow-hidden">
          {rightPane === "ide" ? (
            <EditorPane
              project={project}
              ideUrl={ideUrl}
              projectRoot={projectRoot}
              sessionInFlight={sessionInFlight}
              folder={ideFolder}
            />
          ) : (
            <BrowserPane
              browserSession={browserSession}
              isOpeningBrowser={isOpeningBrowser}
              hasWorkspace={project?.workspace != null}
              awaitingAgent={project?.workspace != null && projectRoot === null}
              onOpenBrowser={handleOpenBrowser}
              mcpToolCallActive={mcpOverlayVisible}
            />
          )}
        </div>
      )}

      {/* Drag overlay — two-panel preview covers real content during drag */}
      {dragging && (
        <div className="absolute inset-0 z-50 flex cursor-col-resize">
          {/* Left preview */}
          <div
            className="flex items-center justify-center bg-white"
            style={{ width: `${dragPct}%` }}
          >
            <span className="icon-[ri--ai] text-5xl text-accent/60" />
          </div>
          {/* Divider preview */}
          <div className="w-2 flex-shrink-0 bg-accent" />
          {/* Right preview */}
          <div className="flex flex-1 items-center justify-center bg-stone-100">
            <span className="icon-[ri--slideshow-2-line] text-5xl text-stone-400" />
          </div>
        </div>
      )}

      {/* Portal-based drawers (position doesn't matter in DOM) */}
      <ProjectSwitcher
        open={switcherOpen}
        onOpenChange={setSwitcherOpen}
        projects={projects}
        activeProjectId={project?.id ?? null}
        onSelect={handleSelectProject}
        onCreate={handleNewProject}
      />
      {project !== null ? (
        <PublishPanel
          open={publishOpen}
          onOpenChange={setPublishOpen}
          projectId={project.id}
          agentBusy={sessionInFlight}
          onRequestPublish={() => handleSendMessage(i18n.t("publish.requestMessage"))}
        />
      ) : null}
      <QuotaDialog error={quotaError} onClose={() => setQuotaError(null)} />
    </main>
  );
}
