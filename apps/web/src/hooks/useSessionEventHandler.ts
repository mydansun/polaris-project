/** `applySessionEvent` extracted from App.tsx.
 *
 * The worker publishes Session/Run/Event-shaped envelopes keyed on
 * `session_id`.  This module holds the reducer logic; App holds the
 * state and passes setters in via the factory.
 */

import { useCallback, useRef } from "react";
import type {
  ClarificationRequest,
  EventResponse,
  ProjectDetailResponse,
  SessionEvent,
} from "@polaris/shared-types";

import type { RightPaneTab, SessionStats } from "../App";
import { msgId, nowIso, TERMINAL_STATUSES, type SessionWithItems } from "../chat/types";

type SetState<T> = (update: T | ((prev: T) => T)) => void;

// StatusBar coalescing window.  Worker already debounces ~500ms server
// side, but low-frequency bursts still arrive as separate frames (one
// inotify event every ~1s each carrying `delta=1`) which read as a
// chain of "+1 +1 +1" flashes.  Collapse frames inside this window into
// one "+N" animation so the count pulse matches the magnitude of change.
// 400ms is tight enough that a single ticked-up delta feels instant and
// loose enough to eat a 3–4 event burst.
const STATS_COALESCE_MS = 400;

type StatsBuffer = {
  fileDelta: number;
  testDelta: number;
  fileCount: number;
  testCount: number;
};

export type SessionEventHandlerDeps = {
  setSessions: SetState<SessionWithItems[]>;
  setProject: SetState<ProjectDetailResponse | null>;
  setClarificationRequest: SetState<ClarificationRequest | null>;
  setClarificationSessionId: SetState<string | null>;
  setClarificationRunId: SetState<string | null>;
  setIsStreamingAgentMsg: SetState<boolean>;
  setPendingPlanApproval: SetState<boolean>;
  /** Flip the right panel (browser / ide / hide).  Used by the
   * `browser_focus_requested` SSE fired from the `focus_browser`
   * dynamic tool. */
  setRightPane: SetState<RightPaneTab>;
  /** StatusBar counters (files / playwright calls) + animation trigger. */
  setSessionStats: SetState<SessionStats>;
  /** Close the EventSource when the session terminates. */
  onSessionTerminal: () => void;
};

export function useSessionEventHandler(
  deps: SessionEventHandlerDeps,
): (raw: unknown) => void {
  const {
    setSessions,
    setProject,
    setClarificationRequest,
    setClarificationSessionId,
    setClarificationRunId,
    setIsStreamingAgentMsg,
    setPendingPlanApproval,
    setRightPane,
    setSessionStats,
    onSessionTerminal,
  } = deps;

  // Leading-edge-with-trailing-flush throttle buffer for session_stats
  // animation.  Events within a STATS_COALESCE_MS window get their deltas
  // summed into one "+N" animation rather than N chained "+1" flashes.
  const statsBufferRef = useRef<StatsBuffer>({
    fileDelta: 0,
    testDelta: 0,
    fileCount: 0,
    testCount: 0,
  });
  const statsTimerRef = useRef<number | null>(null);

  const flushStats = useCallback(() => {
    const buf = statsBufferRef.current;
    if (buf.fileDelta === 0 && buf.testDelta === 0) return;
    const fileDelta = buf.fileDelta;
    const testDelta = buf.testDelta;
    const fileCount = buf.fileCount;
    const testCount = buf.testCount;
    buf.fileDelta = 0;
    buf.testDelta = 0;
    setSessionStats((prev) => ({
      fileChanges: fileCount,
      testCalls: testCount,
      fileDelta,
      testDelta,
      // flashKey bump forces React to re-key the number + "+N" spans so
      // the CSS keyframes restart even if the previous animation is
      // still in flight.
      flashKey: prev.flashKey + 1,
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return useCallback(
    function applySessionEvent(raw: unknown) {
      const src = raw as Record<string, unknown> | null;
      if (!src || typeof src !== "object") return;
      if (typeof src.session_id !== "string") return;
      const event = src as unknown as SessionEvent;

      if (event.kind === "clarification_requested") {
        setClarificationRequest(event.request);
        setClarificationSessionId(event.session_id);
        setClarificationRunId(event.run_id ?? null);
        return;
      }

      if (event.kind === "clarification_answered") {
        setClarificationRequest(null);
        setClarificationSessionId(null);
        setClarificationRunId(null);
        return;
      }

      if (event.kind === "project_root_changed") {
        setProject((current) => {
          if (current === null || current.workspace === null) return current;
          return {
            ...current,
            workspace: { ...current.workspace, project_root: event.path },
          };
        });
        return;
      }

      if (event.kind === "browser_focus_requested") {
        // Agent called `focus_browser` — flip the right pane so the user
        // can watch the upcoming playwright interaction live.  Idempotent
        // on the frontend side; the agent is expected to call it at most
        // once per session before its first playwright MCP call.
        setRightPane("browser");
        return;
      }

      if (event.kind === "session_stats_updated") {
        // Server already debounces ~500ms, but low-frequency bursts
        // still arrive as a stream of delta=1 frames.  We apply a
        // leading-edge throttle here so the first frame animates
        // immediately and subsequent frames within STATS_COALESCE_MS
        // sum into one trailing "+N" flash — avoids chained "+1 +1 +1"
        // animations that read as noise.  Absolute counts tracked in
        // the buffer always reflect the latest event (last-write-wins).
        const buf = statsBufferRef.current;
        buf.fileDelta += event.file_change_delta;
        buf.testDelta += event.playwright_call_delta;
        buf.fileCount = event.file_change_count;
        buf.testCount = event.playwright_call_count;

        if (statsTimerRef.current === null) {
          // Leading edge — fire the first flash instantly.
          flushStats();
          statsTimerRef.current = window.setTimeout(() => {
            statsTimerRef.current = null;
            flushStats();
          }, STATS_COALESCE_MS);
        }
        return;
      }

      if (event.kind === "agent_message_delta") {
        // Token-streamed agent message — append to the newest in-flight
        // codex:agent_message item so AgentMessageBody re-renders live.
        // `event_completed` later overwrites with the authoritative text;
        // any delta drift self-heals.
        setIsStreamingAgentMsg(true);
        setSessions((prev) => {
          const index = prev.findIndex((entry) => entry.session.id === event.session_id);
          if (index === -1) return prev;
          const entry = prev[index];
          let targetIdx = -1;
          for (let i = entry.items.length - 1; i >= 0; i -= 1) {
            const it = entry.items[i];
            if (it.kind === "codex:agent_message" && it.status === "started") {
              targetIdx = i;
              break;
            }
          }
          if (targetIdx === -1) return prev;
          const target = entry.items[targetIdx];
          const prevText =
            typeof target.payload_jsonb.text === "string"
              ? target.payload_jsonb.text
              : "";
          const updated: EventResponse = {
            ...target,
            payload_jsonb: { ...target.payload_jsonb, text: prevText + event.text },
            updated_at: nowIso(),
          };
          const nextItems = entry.items.map((it, i) =>
            i === targetIdx ? updated : it,
          );
          return prev.map((e, i) =>
            i === index ? { ...entry, items: nextItems } : e,
          );
        });
        return;
      }

      setSessions((prev) => {
        const index = prev.findIndex((entry) => entry.session.id === event.session_id);
        if (index === -1) return prev;
        const entry = prev[index];

        if (event.kind === "session_started") {
          const nextEntry: SessionWithItems = {
            ...entry,
            session: {
              ...entry.session,
              status: "running",
              started_at: entry.session.started_at ?? nowIso(),
            },
          };
          return prev.map((e, i) => (i === index ? nextEntry : e));
        }

        if (event.kind === "run_started" || event.kind === "run_completed") {
          // Run boundaries are structural; not rendered as chat bubbles.
          return prev;
        }

        if (event.kind === "event_started") {
          const existingIdx =
            event.external_id === null
              ? -1
              : entry.items.findIndex(
                  (it) => it.external_id === event.external_id,
                );
          const placeholder: EventResponse = {
            id: `tmp-${event.sequence}-${msgId()}`,
            run_id: event.run_id,
            sequence: event.sequence,
            external_id: event.external_id,
            kind: event.event_kind,
            status: "started",
            payload_jsonb: event.payload,
            created_at: nowIso(),
            updated_at: nowIso(),
          };
          const nextItems =
            existingIdx === -1
              ? [...entry.items, placeholder]
              : entry.items.map((it, i) => (i === existingIdx ? placeholder : it));
          return prev.map((e, i) =>
            i === index ? { ...entry, items: nextItems } : e,
          );
        }

        if (event.kind === "event_completed") {
          if (event.event_kind === "codex:agent_message") {
            setIsStreamingAgentMsg(false);
          }
          const existingIdx =
            event.external_id === null
              ? -1
              : entry.items.findIndex(
                  (it) => it.external_id === event.external_id,
                );
          if (existingIdx === -1) {
            const placeholder: EventResponse = {
              id: `tmp-${msgId()}`,
              run_id: event.run_id,
              sequence: entry.items.length + 1,
              external_id: event.external_id,
              kind: event.event_kind,
              status: "completed",
              payload_jsonb: event.payload,
              created_at: nowIso(),
              updated_at: nowIso(),
            };
            return prev.map((e, i) =>
              i === index ? { ...entry, items: [...entry.items, placeholder] } : e,
            );
          }
          const existing = entry.items[existingIdx];
          const updated: EventResponse = {
            ...existing,
            kind: event.event_kind,
            status: "completed",
            payload_jsonb: event.payload,
            updated_at: nowIso(),
          };
          const nextItems = entry.items.map((it, i) =>
            i === existingIdx ? updated : it,
          );
          return prev.map((e, i) =>
            i === index ? { ...entry, items: nextItems } : e,
          );
        }

        if (event.kind === "session_completed") {
          setIsStreamingAgentMsg(false);
          const nextEntry: SessionWithItems = {
            ...entry,
            session: {
              ...entry.session,
              status: event.status,
              error_message: event.error,
              final_message: event.final_message,
              finished_at: nowIso(),
            },
          };
          return prev.map((e, i) => (i === index ? nextEntry : e));
        }

        return prev;
      });

      if (
        event.kind === "session_completed" &&
        TERMINAL_STATUSES.includes(event.status)
      ) {
        onSessionTerminal();
      }

      // Detect plan-round completion: surface the Proceed button when the
      // session ended successfully and at least one codex:plan event landed.
      if (event.kind === "session_completed" && event.status === "completed") {
        setSessions((prev) => {
          const entry = prev.find((e) => e.session.id === event.session_id);
          if (entry && entry.items.some((it) => it.kind === "codex:plan")) {
            setPendingPlanApproval(true);
          }
          return prev;
        });
      }
    },
    // Setter refs are stable — intentionally omit them from deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
}
