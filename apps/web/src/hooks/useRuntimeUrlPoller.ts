/** 30s fallback poller for workspace runtime URLs + project_root.
 *
 * The primary path for "runtime URLs became available" is the SSE event
 * `project_root_changed` — when that fires, the reducer updates
 * `project.workspace.project_root` in state and the main project-load
 * effect in App.tsx refetches runtime + browser session.
 *
 * This poller is a belt-and-suspenders fallback for cases where SSE is
 * unreliable or absent (session not active, transient disconnect, etc.).
 * It runs ONLY while there's a project without a project_root.  It hits
 * one endpoint — `/workspace/runtime` — which carries `ide_url`,
 * `browser_url`, and (once set) `project_root`.  Writing `project_root`
 * back into project state flips the gate, the main effect fetches the
 * browser session once, and the poller clears itself.
 */

import { useEffect } from "react";
import type { ProjectDetailResponse } from "@polaris/shared-types";

import { getWorkspaceRuntime } from "../api";

const POLL_INTERVAL_MS = 30_000;

type SetProject = (
  update:
    | ProjectDetailResponse
    | null
    | ((prev: ProjectDetailResponse | null) => ProjectDetailResponse | null),
) => void;

export function useRuntimeUrlPoller(
  project: ProjectDetailResponse | null,
  setProject: SetProject,
): void {
  const projectId = project?.id ?? null;
  const projectRoot = project?.workspace?.project_root ?? null;

  useEffect(() => {
    if (projectId === null || projectRoot !== null) return;

    let alive = true;
    const tick = async () => {
      try {
        const runtime = await getWorkspaceRuntime(projectId);
        if (!alive) return;
        if (runtime.project_root === null && runtime.ide_url === null) return;
        setProject((current) => {
          if (
            current === null
            || current.id !== projectId
            || current.workspace === null
          ) {
            return current;
          }
          return {
            ...current,
            workspace: {
              ...current.workspace,
              ide_url: runtime.ide_url,
              ide_status: runtime.status,
              project_root: runtime.project_root,
            },
          };
        });
      } catch {
        /* transient — next tick will retry */
      }
    };

    const id = setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [projectId, projectRoot, setProject]);
}
