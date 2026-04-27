import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import type {
  BrowserSessionResponse,
  ClarificationRecord,
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
  deleteProject,
  ensureBrowserSession,
  ensureWorkspaceIdeSession,
  ensureWorkspaceRuntime,
  steerSession,
  restartWorkspaceRuntime,
  getBrowserSession,
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
import { ProjectSwitcher } from "./ProjectSwitcher";
import { QuotaDialog } from "./QuotaDialog";
import {
  TERMINAL_STATUSES,
  SESSIONS_PAGE_SIZE,
  WORKSPACE_CONTAINER_PATH,
  buildMessages,
  nowIso,
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

/**
 * The chat + IDE + browser shell, scoped to one project (or to the
 * "no project yet" welcome state on /projects/new).  Project identity
 * is URL-driven via ``useParams``; switching projects = navigating.
 *
 * Auth is handled by the parent <App>; this component never sees a
 * null user.
 */
export function ProjectAppShell({
  user,
  onLogout,
}: {
  user: UserResponse;
  onLogout: () => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const params = useParams<{ projectId?: string }>();
  const isNewRoute = location.pathname === "/projects/new";
  const urlProjectId = params.projectId ?? null;

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
  // ID of the project currently being deleted.  When set, the entire
  // shell renders a full-screen overlay and effects bail out — the
  // workspace runtime tears down server-side as we wait, so any
  // in-flight polls or SSE would surface confusing errors otherwise.
  const [deletingProjectId, setDeletingProjectId] = useState<string | null>(null);
  // Per-session steer messages (extra user input typed while codex was
  // already running).  The codex app-server forwards them straight
  // into the active turn over WS; it does NOT emit them back as a
  // ``codex:userMessage`` item (that kind is mapped to None in
  // ``_map_kind``), so we have to keep them client-side and merge
  // them into the chat stream ourselves.  In-memory only — refreshing
  // the tab loses them, but the codex thread on the backend has them
  // baked into its conversation state, so the agent's behavior is
  // unaffected.
  const [steersBySessionId, setSteersBySessionId] = useState<
    Record<string, import("./chat/types").SteeredMessage[]>
  >({});
  // Suppress the workspace-starting overlay for a brief grace period so
  // warm workspaces (which finish ensureWorkspaceRuntime in ~300 ms)
  // never flash the overlay — the flash reads as a glitch.  The drawer
  // switch path didn't have this problem because ProjectAppShell stays
  // mounted across switches and ``project`` retains the previous
  // project's value while the new one loads; the HomePage→project path
  // remounts the shell with project=null, which is what makes the
  // overlay matter on cold starts and look broken on warm ones.
  const [showStartingOverlay, setShowStartingOverlay] = useState(false);
  useEffect(() => {
    if (urlProjectId === null || project !== null) {
      setShowStartingOverlay(false);
      return;
    }
    const timer = window.setTimeout(() => setShowStartingOverlay(true), 600);
    return () => window.clearTimeout(timer);
  }, [urlProjectId, project]);
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

  // Steer is only safe while CODEX is the live agent.  Discovery's
  // langgraph control handler ignores ``kind:"steer"`` (the graph is
  // structurally rigid; mid-graph injection would tear state), so
  // typing during a discovery turn would just no-op silently.  We
  // detect "codex is now running" by the presence of any ``codex:*``
  // item in the active session — discovery emits ``discovery:*``
  // items first; once codex starts, its items appear and we know the
  // discovery agent is done.
  const canSteer =
    sessionInFlight &&
    activeSession !== null &&
    sessions.some((entry) =>
      entry.session.id === activeSession.id &&
      entry.items.some((item) => item.kind.startsWith("codex:")),
    );

  const handleSteer = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (trimmed.length === 0 || activeSession === null) return;
      const sessionId = activeSession.id;
      // Optimistic insert: show the bubble before the network round-trip
      // so the chat doesn't feel laggy when the user is typing fast.
      const optimistic = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        text: trimmed,
        timestamp: new Date().toISOString(),
      };
      setSteersBySessionId((prev) => ({
        ...prev,
        [sessionId]: [...(prev[sessionId] ?? []), optimistic],
      }));
      try {
        await steerSession(sessionId, { message: trimmed });
      } catch (err) {
        // Roll back the optimistic bubble if the API rejected (e.g.
        // session terminal-d between the input gate and the call).
        setSteersBySessionId((prev) => ({
          ...prev,
          [sessionId]: (prev[sessionId] ?? []).filter(
            (m) => m.id !== optimistic.id,
          ),
        }));
        setError(err instanceof Error ? err.message : "Unable to send steer");
      }
    },
    [activeSession],
  );

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
  const messages = buildMessages(sessions, steersBySessionId);

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
            const { runs, clarifications, ...rest } = d;
            const items = runs.flatMap((r) => r.events);
            return {
              session: rest as SessionResponse,
              items,
              clarifications: clarifications ?? [],
            };
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
          const { runs, clarifications, ...rest } = d;
          const items = runs.flatMap((r) => r.events);
          return {
            session: rest as SessionResponse,
            items,
            clarifications: clarifications ?? [],
          };
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
        // 404 = the project was deleted (in another tab, by another
        // user, or via a stale shared URL).  Bounce to home rather
        // than dumping the user into an error-only shell with no
        // navigation affordances.
        const msg = err instanceof Error ? err.message : "";
        if (/(?:^|\b)404\b/.test(msg)) {
          navigate("/", { replace: true });
          return;
        }
        setError(msg || "Failed to load project");
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
      // Best-effort prefetch the switcher list (uses recent updated_at
      // ordering for free).  Errors here don't block project loading.
      refreshProjects().catch(() => {});

      // Project identity comes from the URL.  /projects/:id loads that
      // project; /projects/new shows the welcome state with project=null.
      if (urlProjectId) {
        try {
          await handleSelectProject(urlProjectId);
        } catch (restoreError) {
          if (alive) {
            setError(
              restoreError instanceof Error
                ? restoreError.message
                : "Unable to load project",
            );
          }
        }
      }
      // /projects/new is a no-op — project state already starts as null.
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
    // Reset to the "no project" state and navigate to the welcome route.
    // ChatPane's submit handler creates the project on first message,
    // then handleSendMessage navigates to /projects/<id> below.
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
    if (location.pathname !== "/projects/new") {
      navigate("/projects/new");
    }
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
        // Reflect the just-created project in the URL so a refresh stays
        // on the project (instead of bouncing back to /projects/new).
        navigate(`/projects/${currentProject.id}`, { replace: true });
      }

      // Routing rule: discovery must run AT LEAST ONCE successfully
      // before Codex turns work.  We track that via
      // ``project.has_active_design_intent`` (the active row in
      // ``design_intents`` — only written when discovery runs to
      // completion).  ALWAYS refetch the project right before
      // deciding mode — the local ``project`` state was captured at
      // page-load and goes stale once discovery completes mid-session,
      // which would otherwise cause the very-next-message after
      // interrupting codex to redundantly re-trigger discovery.
      //
      // Backend is responsible for prepending prior interrupted
      // user_messages when discover_then_build re-runs (see
      // ``orchestrator._collect_resumed_user_message``); the frontend
      // only sends what the user actually typed so the chat bubble
      // displays exactly that.
      try {
        const fresh = await getProject(currentProject.id);
        currentProject = fresh;
        setProject(fresh);
      } catch {
        // Stale project is recoverable — worst case we mis-route to
        // discover_then_build and the backend dedupes.
      }
      const needsDiscovery = !currentProject.has_active_design_intent;
      const newSession = await createSession(currentProject.id, {
        message: text,
        mode: needsDiscovery ? "discover_then_build" : "build_direct",
      });
      setSessions((prev) => [...prev, { session: newSession, items: [], clarifications: [] }]);
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
        navigate(`/projects/${currentProject.id}`, { replace: true });
      }

      const newSession = await createSession(currentProject.id, {
        message: text,
        mode: "discover_then_build",
      });
      setSessions((prev) => [...prev, { session: newSession, items: [], clarifications: [] }]);
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
      setSessions((prev) => [...prev, { session: newSession, items: [], clarifications: [] }]);
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
    const sessionId = response.session_id ?? clarificationSessionId ?? null;
    const questions = clarificationRequest?.questions ?? [];
    try {
      await submitClarification(project.id, {
        ...response,
        session_id: sessionId ?? undefined,
        run_id: response.run_id ?? clarificationRunId ?? undefined,
      });
      // Immediately surface the user's selections in the chat so the
      // timeline matches what GET /sessions/{id} will render after a
      // page reload.  The backend persists the same questions+answers
      // in the clarifications table; this is just the optimistic local
      // copy that avoids a refetch.
      if (sessionId !== null && questions.length > 0) {
        const ts = nowIso();
        const synthetic: ClarificationRecord = {
          id: response.request_id,
          request_id: response.request_id,
          session_id: sessionId,
          run_id: response.run_id ?? clarificationRunId ?? "",
          agent_kind: clarificationRequest?.source ?? "codex",
          status: "answered",
          questions,
          answers: response.answers,
          created_at: ts,
          answered_at: ts,
        };
        setSessions((prev) =>
          prev.map((entry) =>
            entry.session.id === sessionId
              ? { ...entry, clarifications: [...entry.clarifications, synthetic] }
              : entry,
          ),
        );
      }
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
    setProject(null);
    setSessions([]);
    setActiveSessionId(null);
    setBrowserSession(null);
    onLogout();
    navigate("/", { replace: true });
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

  // Auth gate is in <App>; this component only renders post-auth.
  // ORDER MATTERS: the deletion overlay check must come BEFORE the
  // "loading project" check, because the delete handler sets
  // ``project = null`` to bail out the polling effects — without this
  // ordering the user briefly sees the "starting workspace" copy on
  // delete (urlProjectId is still set + project is null) instead of
  // the actual "deleting" message.
  //
  // Deletion overlay short-circuits the rest of the render so polling
  // effects, SSE handlers, and tool buttons can't fight a shutting-down
  // workspace.  Backdrop is a translucent frosted-glass layer (no
  // solid gray fill) so the previous shell stays softly visible
  // through it — feels like a transient state on top of the project,
  // not a hard "the page is gone".
  if (deletingProjectId !== null) {
    return (
      <main className="flex h-dvh items-center justify-center bg-white/40 backdrop-blur-md">
        <div
          role="status"
          aria-live="polite"
          data-testid="project-deleting-overlay"
          className="flex flex-col items-center gap-3 rounded-xl border border-border-light bg-surface/90 px-8 py-6 shadow-lg backdrop-blur-sm"
        >
          <span className="icon-[lucide--loader-2] animate-spin text-2xl text-text-muted" aria-hidden />
          <p className="text-sm font-medium text-text-primary">
            {i18n.t("projects.deleting")}
          </p>
        </div>
      </main>
    );
  }

  // While loading a specific project (URL has :projectId but state is
  // still null), behavior splits in two:
  //   - First 600 ms: render a blank surface.  Warm workspaces resolve
  //     in ~300 ms and the user perceives this as "instant transition";
  //     anything else (overlay, welcome state) would flash and read as
  //     a glitch.
  //   - After 600 ms: render the explicit "Starting workspace…" overlay
  //     so the user knows we're working on the cold-start case (5–15 s).
  if (urlProjectId !== null && project === null) {
    if (!showStartingOverlay) {
      return <div className="h-dvh bg-surface" />;
    }
    return (
      <div className="flex h-dvh items-center justify-center bg-surface">
        <div
          role="status"
          aria-live="polite"
          data-testid="workspace-starting-overlay"
          className="flex flex-col items-center gap-3 rounded-xl border border-border-light bg-surface-alt px-8 py-6 shadow-sm"
        >
          <span
            className="icon-[lucide--loader-2] animate-spin text-2xl text-text-muted"
            aria-hidden
          />
          <p className="text-sm font-medium text-text-primary">
            {i18n.t("project.startingTitle")}
          </p>
          <p className="max-w-xs text-center text-xs text-text-muted">
            {i18n.t("project.startingBody")}
          </p>
        </div>
      </div>
    );
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
        // Mid-turn steer: enabled while codex is running.  ChatPane keeps
        // the input box live and routes Enter through onSteer instead of
        // onSendMessage when canSteer is true.
        canSteer={canSteer}
        onSteer={handleSteer}
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
        onDeleteProject={async () => {
          if (project === null) return;
          const id = project.id;
          // Drop everything project-scoped: project=null bails out
          // the polling effects (browser session, workspace runtime,
          // SSE) so they don't error against a shutting-down workspace.
          // deletingProjectId flips render to the overlay.
          eventSourceRef.current?.close();
          eventSourceRef.current = null;
          setDeletingProjectId(id);
          setProject(null);
          setSessions([]);
          try {
            await deleteProject(id);
          } catch (err) {
            // Network blip after server commits is common — the API's
            // workspace teardown can interrupt docker networking
            // mid-response.  Verify the outcome before surfacing the
            // error so the user isn't stuck on the spinner.
            try {
              await getProject(id);
              // Project still exists → real failure; bail out.
              setDeletingProjectId(null);
              throw err;
            } catch {
              // 404 → server-side delete succeeded; treat as success.
            }
          }
          setProjects((prev) => prev.filter((p) => p.id !== id));
          navigate("/", { replace: true });
        }}
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
        onSelect={(id) => {
          // Navigate so the URL stays the source of truth; the load
          // effect (or handleSelectProject directly) brings up the
          // session for the freshly-selected project.
          navigate(`/projects/${id}`);
          handleSelectProject(id).catch(() => {});
        }}
        onCreate={handleNewProject}
        onHome={() => navigate("/")}
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
