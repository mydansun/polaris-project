import type {
  BrowserSessionResponse,
  ClarificationRequest,
  ClarificationResponse,
  DeploymentDetailResponse,
  DeploymentResponse,
  ProjectCreatePayload,
  ProjectDetailResponse,
  ProjectResponse,
  ProjectVersionResponse,
  ReadyResponse,
  SessionCreatePayload,
  SessionDetailResponse,
  SessionResponse,
  SessionSteerPayload,
  SnapshotCreatePayload,
  UserResponse,
  WorkspaceFileContent,
  WorkspaceFileEntry,
  WorkspaceFileWritePayload,
  WorkspaceIdeSessionResponse,
  WorkspaceRuntimeRequest,
  WorkspaceRuntimeResponse,
} from "@polaris/shared-types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
};

export type QuotaReason = "global_quota" | "user_quota";

/** Thrown when the API rejects session creation with HTTP 429 because the
 * per-user or global concurrent-run limit is already in use.  Callers catch
 * this to pop the QuotaDialog instead of the generic error banner. */
export class QuotaError extends Error {
  constructor(public readonly reason: QuotaReason, public readonly limit: number) {
    super(reason);
    this.name = "QuotaError";
  }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? "GET",
    credentials: "include",
    headers: options.body === undefined ? undefined : { "Content-Type": "application/json" },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  if (!response.ok) {
    if (response.status === 429) {
      const detail = (await response.json().catch(() => null)) as
        | { detail?: { reason?: string; limit?: number } }
        | null;
      const reason = detail?.detail?.reason;
      if (reason === "global_quota" || reason === "user_quota") {
        throw new QuotaError(reason, detail?.detail?.limit ?? 0);
      }
    }
    const body = await response.text();
    throw new Error(body || `Request failed with ${response.status}`);
  }

  return response.json() as Promise<T>;
}

export function getReady(): Promise<ReadyResponse> {
  return request<ReadyResponse>("/ready");
}

export function listProjects(): Promise<ProjectResponse[]> {
  return request<ProjectResponse[]>("/projects");
}

export function getProject(projectId: string): Promise<ProjectDetailResponse> {
  return request<ProjectDetailResponse>(`/projects/${projectId}`);
}

export function createProject(payload: ProjectCreatePayload): Promise<ProjectDetailResponse> {
  return request<ProjectDetailResponse>("/projects", { method: "POST", body: payload });
}

// ─── Sessions (one per user message; internally aggregates agent runs) ────

export function listProjectSessions(
  projectId: string,
  opts?: { limit?: number; beforeSequence?: number },
): Promise<SessionResponse[]> {
  const params = new URLSearchParams();
  if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts?.beforeSequence !== undefined) params.set("before_sequence", String(opts.beforeSequence));
  const qs = params.toString();
  return request<SessionResponse[]>(`/projects/${projectId}/sessions${qs ? `?${qs}` : ""}`);
}

export function createSession(
  projectId: string,
  payload: SessionCreatePayload,
): Promise<SessionResponse> {
  return request<SessionResponse>(`/projects/${projectId}/sessions`, {
    method: "POST",
    body: payload,
  });
}

export function getSession(sessionId: string): Promise<SessionDetailResponse> {
  return request<SessionDetailResponse>(`/sessions/${sessionId}`);
}

export function interruptSession(sessionId: string): Promise<SessionResponse> {
  return request<SessionResponse>(`/sessions/${sessionId}/interrupt`, { method: "POST" });
}

export function steerSession(
  sessionId: string,
  payload: SessionSteerPayload,
): Promise<SessionResponse> {
  return request<SessionResponse>(`/sessions/${sessionId}/steer`, {
    method: "POST",
    body: payload,
  });
}

export function submitClarification(
  projectId: string,
  body: ClarificationResponse,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/projects/${projectId}/clarify/response`, { method: "POST", body });
}

export function getPendingClarification(
  projectId: string,
): Promise<{ pending: ClarificationRequest | null }> {
  return request<{ pending: ClarificationRequest | null }>(
    `/projects/${projectId}/clarify/pending`,
  );
}


export function subscribeSessionEvents(
  sessionId: string,
  onEvent: (event: unknown) => void,
  onError: (error: Event) => void,
): EventSource {
  const source = new EventSource(`${API_BASE_URL}/sessions/${sessionId}/events`, {
    withCredentials: true,
  });
  source.onmessage = (event) => {
    if (!event.data || event.data.startsWith(":")) return;
    try {
      onEvent(JSON.parse(event.data));
    } catch {
      /* ignore malformed frames */
    }
  };
  source.addEventListener("error", onError);
  return source;
}

// ─── Workspace files / runtime / exec (unchanged surface) ─────────────────

export function listWorkspaceFiles(projectId: string): Promise<WorkspaceFileEntry[]> {
  return request<WorkspaceFileEntry[]>(`/projects/${projectId}/workspace/files`);
}

export function getWorkspaceFile(projectId: string, path: string): Promise<WorkspaceFileContent> {
  return request<WorkspaceFileContent>(
    `/projects/${projectId}/workspace/files/content?path=${encodeURIComponent(path)}`,
  );
}

export function writeWorkspaceFile(
  projectId: string,
  payload: WorkspaceFileWritePayload,
): Promise<WorkspaceFileContent> {
  return request<WorkspaceFileContent>(`/projects/${projectId}/workspace/files/content`, {
    method: "PUT",
    body: payload,
  });
}

export function createWorkspaceSnapshot(
  projectId: string,
  payload: SnapshotCreatePayload,
): Promise<ProjectVersionResponse> {
  return request<ProjectVersionResponse>(`/projects/${projectId}/workspace/snapshot`, {
    method: "POST",
    body: payload,
  });
}

export function listWorkspaceVersions(projectId: string): Promise<ProjectVersionResponse[]> {
  return request<ProjectVersionResponse[]>(`/projects/${projectId}/workspace/versions`);
}

export function ensureWorkspaceIdeSession(projectId: string): Promise<WorkspaceIdeSessionResponse> {
  return request<WorkspaceIdeSessionResponse>(`/projects/${projectId}/workspace/ide/session`, {
    method: "POST",
  });
}

export function getWorkspaceRuntime(projectId: string): Promise<WorkspaceRuntimeResponse> {
  return request<WorkspaceRuntimeResponse>(`/projects/${projectId}/workspace/runtime`);
}

export function ensureWorkspaceRuntime(
  projectId: string,
  payload: WorkspaceRuntimeRequest = {},
): Promise<WorkspaceRuntimeResponse> {
  return request<WorkspaceRuntimeResponse>(`/projects/${projectId}/workspace/runtime`, {
    method: "POST",
    body: payload,
  });
}

export function restartWorkspaceRuntime(
  projectId: string,
  payload: WorkspaceRuntimeRequest = {},
): Promise<WorkspaceRuntimeResponse> {
  return request<WorkspaceRuntimeResponse>(
    `/projects/${projectId}/workspace/runtime/restart`,
    { method: "POST", body: payload },
  );
}

/** GET /browser/session returns 204 No Content when the agent hasn't declared
 * project_root yet OR no session exists — treat that as "not yet" rather
 * than an error so polling stays quiet in the devtools network panel. */
export async function getBrowserSession(
  projectId: string,
): Promise<BrowserSessionResponse | null> {
  const response = await fetch(`${API_BASE_URL}/projects/${projectId}/browser/session`, {
    credentials: "include",
  });
  if (response.status === 204) return null;
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Request failed with ${response.status}`);
  }
  return (await response.json()) as BrowserSessionResponse;
}

export function ensureBrowserSession(projectId: string): Promise<BrowserSessionResponse> {
  return request<BrowserSessionResponse>(`/projects/${projectId}/browser/session`, {
    method: "POST",
  });
}

export function stopBrowserSession(projectId: string): Promise<BrowserSessionResponse> {
  return request<BrowserSessionResponse>(`/projects/${projectId}/browser/session`, {
    method: "DELETE",
  });
}

export function requestCode(
  email: string,
  inviteCode?: string,
): Promise<{ ok: boolean; reason?: string }> {
  return request<{ ok: boolean; reason?: string }>("/auth/request-code", {
    method: "POST",
    body: { email, invite_code: inviteCode || undefined },
  });
}

export function verifyCode(email: string, code: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/auth/verify-code", {
    method: "POST",
    body: { email, code },
  });
}

export function getDevLoginUrl(): string {
  return `${API_BASE_URL}/auth/dev-login`;
}

/** Unauthenticated capability probe — tells the LoginPage whether the
 *  one-click Dev Login shortcut is enabled on this instance.  The
 *  backend returns `{dev_login_enabled: false}` when
 *  `POLARIS_DEV_USER_EMAIL` is empty (staging / prod default). */
export function getAuthConfig(): Promise<{ dev_login_enabled: boolean }> {
  return request<{ dev_login_enabled: boolean }>("/auth/config");
}

export function getMe(): Promise<UserResponse> {
  return request<UserResponse>("/auth/me");
}

// ─── Publish / Deployments ────────────────────────────────────────────────

export function publishProject(projectId: string): Promise<DeploymentResponse> {
  return request<DeploymentResponse>(`/projects/${projectId}/publish`, {
    method: "POST",
    body: {},
  });
}

export function listDeployments(
  projectId: string,
  limit = 20,
): Promise<DeploymentResponse[]> {
  return request<DeploymentResponse[]>(
    `/projects/${projectId}/deployments?limit=${limit}`,
  );
}

export function getDeployment(deploymentId: string): Promise<DeploymentDetailResponse> {
  return request<DeploymentDetailResponse>(`/deployments/${deploymentId}`);
}

export function subscribeDeploymentEvents(
  deploymentId: string,
  onEvent: (event: unknown) => void,
  onError: (error: Event) => void,
): EventSource {
  const source = new EventSource(
    `${API_BASE_URL}/deployments/${deploymentId}/events`,
    { withCredentials: true },
  );
  source.onmessage = (event) => {
    if (!event.data || event.data.startsWith(":")) return;
    try {
      onEvent(JSON.parse(event.data));
    } catch {
      /* ignore malformed frames */
    }
  };
  source.addEventListener("error", onError);
  return source;
}

export function rollbackDeployment(
  projectId: string,
  gitCommitHash: string,
): Promise<DeploymentResponse> {
  return request<DeploymentResponse>(`/projects/${projectId}/rollback`, {
    method: "POST",
    body: { git_commit_hash: gitCommitHash },
  });
}

export function logout(): Promise<void> {
  return request<void>("/auth/logout", { method: "POST" });
}
