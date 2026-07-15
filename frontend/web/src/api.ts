import type {
  AgentContextPayload,
  AgentFileChangesPayload,
  AgentFileDiffPayload,
  AgentFileRevertPayload,
  ChatSummary,
  DirectoryPayload,
  ExtensionsPayload,
  FileEntry,
  PendingContextPayload,
  PinsPayload,
  PreviewPayload,
  ChatSection,
  ScheduleAcceptOverrides,
  ScheduleTask,
  SessionPayload,
  SidebarSection,
  TreeNode,
  WorkspaceOverviewPayload
} from "./types";
import {
  errorMessageFromPayload,
  parseAgentFileChangesPayload,
  parseAgentFileDiffPayload,
  parseAgentFileRevertPayload,
  parseChatSummaries,
  parseChatSummary,
  parseDirectoryPayload,
  parseExtensionsPayload,
  parseOkPayload,
  parsePathPayload,
  parsePathsPayload,
  parsePendingContextPayload,
  parsePinsPayload,
  parsePreviewPayload,
  parseScheduleTaskEnvelope,
  parseScheduleTaskList,
  parseSearchPayload,
  parseSessionPayload,
  parseTreeNode,
  parseUploadPayload,
  parseWebSocketTicketPayload,
  parseWorkspaceOverviewPayload
} from "./contracts";
import { REQUEST_FAILED_LABEL, UPLOAD_FAILED_LABEL } from "./i18n/ru";
import type { UploadPayload } from "./contracts";

type ApiOptions = RequestInit & {
  csrf?: string;
};

async function readJson(response: Response): Promise<unknown> {
  return response.json().catch(() => ({}));
}

async function parseJson<T>(response: Response, parser: (value: unknown) => T): Promise<T> {
  const payload = await readJson(response);
  if (!response.ok) {
    throw new Error(errorMessageFromPayload(payload) || REQUEST_FAILED_LABEL);
  }
  return parser(payload);
}

export function apiFetch<T>(
  path: string,
  parser: (value: unknown) => T,
  options: ApiOptions = {}
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.csrf) {
    headers.set("X-CSRF-Token", options.csrf);
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(path, {
    ...options,
    headers
  }).then((response) => parseJson(response, parser));
}

export function getSession(): Promise<SessionPayload> {
  return apiFetch("/api/session", parseSessionPayload);
}

export function login(username: string, password: string): Promise<SessionPayload> {
  return apiFetch("/api/login", parseSessionPayload, {
    method: "POST",
    body: JSON.stringify({ username, password })
  });
}

export function logout(csrf: string): Promise<{ ok: boolean }> {
  return apiFetch("/api/logout", parseOkPayload, {
    method: "POST",
    csrf
  });
}

export function createWebSocketTicket(
  csrf: string
): Promise<{ ticket: string; expires_in_seconds: number }> {
  return apiFetch("/api/ws-ticket", parseWebSocketTicketPayload, {
    method: "POST",
    csrf
  });
}

export function getWorkspaceOverview(): Promise<WorkspaceOverviewPayload> {
  return apiFetch("/api/workspace/overview", parseWorkspaceOverviewPayload);
}

// --- Etap 2: chat history endpoints ---

export function getChats(csrf: string, section?: ChatSection): Promise<ChatSummary[]> {
  const params = section ? new URLSearchParams({ section }) : new URLSearchParams();
  const qs = params.toString();
  return apiFetch(`/api/chats${qs ? `?${qs}` : ""}`, (value) =>
    parseChatSummaries((value as { chats?: unknown }).chats)
  );
}

export function createChat(csrf: string, section: SidebarSection): Promise<ChatSummary> {
  return apiFetch("/api/chats", parseChatEnvelope, {
    method: "POST",
    csrf,
    body: JSON.stringify({ section })
  });
}

export function activateChat(csrf: string, chatId: number): Promise<ChatSummary> {
  return apiFetch(`/api/chats/${chatId}/activate`, parseChatEnvelope, {
    method: "POST",
    csrf
  });
}

// --- Etap 2B: chat management ---

export function renameChat(csrf: string, chatId: number, title: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/chats/${chatId}`, parseOkPayload, {
    method: "PATCH",
    csrf,
    body: JSON.stringify({ title })
  });
}

export function deleteChat(csrf: string, chatId: number): Promise<{ ok: boolean }> {
  return apiFetch(`/api/chats/${chatId}`, parseOkPayload, {
    method: "DELETE",
    csrf
  });
}

// --- B-140 / B-141: schedule lifecycle ---

export function listSchedule(
  csrf: string,
  statuses?: string[]
): Promise<ScheduleTask[]> {
  const params = new URLSearchParams();
  if (statuses && statuses.length > 0) {
    params.set("status", statuses.join(","));
  }
  const qs = params.toString();
  return apiFetch(`/api/schedule${qs ? `?${qs}` : ""}`, parseScheduleTaskList, {
    csrf
  });
}

export function getScheduleTask(csrf: string, taskId: string): Promise<ScheduleTask> {
  return apiFetch(`/api/schedule/${encodeURIComponent(taskId)}`, parseScheduleTaskEnvelope, {
    csrf
  });
}

export function acceptSchedule(
  csrf: string,
  taskId: string,
  overrides: ScheduleAcceptOverrides = {}
): Promise<ScheduleTask> {
  return apiFetch(
    `/api/schedule/${encodeURIComponent(taskId)}/accept`,
    parseScheduleTaskEnvelope,
    {
      method: "POST",
      csrf,
      body: JSON.stringify(overrides)
    }
  );
}

export function dismissSchedule(csrf: string, taskId: string): Promise<ScheduleTask> {
  return apiFetch(
    `/api/schedule/${encodeURIComponent(taskId)}/dismiss`,
    parseScheduleTaskEnvelope,
    {
      method: "POST",
      csrf
    }
  );
}

export function pauseSchedule(csrf: string, taskId: string): Promise<ScheduleTask> {
  return apiFetch(
    `/api/schedule/${encodeURIComponent(taskId)}/pause`,
    parseScheduleTaskEnvelope,
    {
      method: "POST",
      csrf
    }
  );
}

export function resumeSchedule(csrf: string, taskId: string): Promise<ScheduleTask> {
  return apiFetch(
    `/api/schedule/${encodeURIComponent(taskId)}/resume`,
    parseScheduleTaskEnvelope,
    {
      method: "POST",
      csrf
    }
  );
}

// --- Etap 4: Extensions management ---

export function getExtensions(): Promise<ExtensionsPayload> {
  return apiFetch("/api/extensions", parseExtensionsPayload);
}

export function reloadExtensions(csrf: string): Promise<{ ok: boolean; errors?: string[] }> {
  return apiFetch("/api/extensions/reload", parseOkPayload, {
    method: "POST",
    csrf
  });
}

// --- Etap 5: Agent Context ---

export function getAgentContext(): Promise<AgentContextPayload> {
  return apiFetch("/api/agent-context", (value) => ({
    instructions: typeof (value as { instructions?: unknown }).instructions === "string"
      ? (value as { instructions: string }).instructions
      : "",
    tone: ["default", "concise", "detailed"].includes(
      (value as { tone?: string }).tone ?? ""
    )
      ? ((value as { tone: AgentContextPayload["tone"] }).tone)
      : "default"
  }));
}

export function saveAgentContext(
  csrf: string,
  payload: AgentContextPayload
): Promise<{ ok: boolean }> {
  return apiFetch("/api/agent-context", parseOkPayload, {
    method: "PUT",
    csrf,
    body: JSON.stringify(payload)
  });
}

export function getAgentContextPreview(): Promise<{ prompt: string }> {
  return apiFetch("/api/agent-context/preview", (value) => ({
    prompt: typeof (value as { prompt?: unknown }).prompt === "string"
      ? (value as { prompt: string }).prompt
      : ""
  }));
}

/** POST /api/chats and POST /api/chats/{id}/activate return `{chat: {...}}`. */
function parseChatEnvelope(value: unknown): ChatSummary {
  const source = (value ?? {}) as { chat?: unknown };
  return parseChatSummary(source.chat);
}

export function listFiles(
  path: string,
  sort = "name",
  order = "asc"
): Promise<DirectoryPayload> {
  const params = new URLSearchParams({ path, sort, order });
  return apiFetch(`/api/files?${params.toString()}`, parseDirectoryPayload);
}

export function searchFiles(query: string): Promise<{ query: string; entries: FileEntry[] }> {
  const params = new URLSearchParams({ query, limit: "200" });
  return apiFetch(`/api/files/search?${params}`, parseSearchPayload);
}

export function loadTree(): Promise<TreeNode> {
  return apiFetch("/api/files/tree?depth=4", parseTreeNode);
}

export function previewFile(path: string): Promise<PreviewPayload> {
  return apiFetch(`/api/files/preview?path=${encodeURIComponent(path)}`, parsePreviewPayload);
}

export function makeDirectory(csrf: string, path: string, name: string): Promise<{ path: string }> {
  return apiFetch("/api/files/mkdir", parsePathPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ path, name })
  });
}

export function renameFile(
  csrf: string,
  path: string,
  newName: string
): Promise<{ path: string }> {
  return apiFetch("/api/files/rename", parsePathPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ path, new_name: newName })
  });
}

export function moveFiles(
  csrf: string,
  paths: string[],
  targetDir: string
): Promise<{ paths: string[] }> {
  return apiFetch("/api/files/move", parsePathsPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ paths, target_dir: targetDir })
  });
}

export function copyFiles(
  csrf: string,
  paths: string[],
  targetDir: string
): Promise<{ paths: string[] }> {
  return apiFetch("/api/files/copy", parsePathsPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ paths, target_dir: targetDir })
  });
}

export function deleteFiles(
  csrf: string,
  paths: string[],
  recursive: boolean
): Promise<{ paths: string[] }> {
  return apiFetch("/api/files/delete", parsePathsPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ paths, recursive })
  });
}

export function downloadUrl(path: string): string {
  return `/api/files/download?path=${encodeURIComponent(path)}`;
}

/** B-094: one-shot attach to next message. */
export function attachContext(
  csrf: string,
  path: string,
  sessionId: number,
  options?: { baselineTokens?: number; chunked?: boolean | null }
): Promise<PendingContextPayload> {
  const body: Record<string, unknown> = { path, session_id: sessionId };
  if (options?.baselineTokens !== undefined) body.baseline_tokens = options.baselineTokens;
  if (options?.chunked !== undefined) body.chunked = options.chunked;
  return apiFetch("/api/files/attach-context", parsePendingContextPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify(body)
  });
}

export function detachContext(
  csrf: string,
  sessionId: number,
  path?: string
): Promise<PendingContextPayload> {
  const body: Record<string, unknown> =
    path === undefined ? { all: true, session_id: sessionId } : { path, session_id: sessionId };
  return apiFetch("/api/files/detach-context", parsePendingContextPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify(body)
  });
}

export function listPendingContext(
  sessionId: number
): Promise<PendingContextPayload> {
  return apiFetch(
    `/api/files/pending-context?session_id=${encodeURIComponent(String(sessionId))}`,
    parsePendingContextPayload
  );
}

/** B-095: sticky pin (≤25% context). */
export function pinContext(
  csrf: string,
  path: string,
  sessionId: number,
  options?: { chunked?: boolean | null }
): Promise<PinsPayload> {
  const body: Record<string, unknown> = { path, session_id: sessionId };
  if (options?.chunked !== undefined) body.chunked = options.chunked;
  return apiFetch("/api/files/pin-context", parsePinsPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify(body)
  });
}

export function unpinContext(
  csrf: string,
  sessionId: number,
  path?: string
): Promise<PinsPayload> {
  const body: Record<string, unknown> =
    path === undefined ? { all: true, session_id: sessionId } : { path, session_id: sessionId };
  return apiFetch("/api/files/unpin-context", parsePinsPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify(body)
  });
}

export function listPins(sessionId: number): Promise<PinsPayload> {
  return apiFetch(
    `/api/files/pins?session_id=${encodeURIComponent(String(sessionId))}`,
    parsePinsPayload
  );
}

/** B-117: agent file change journal. */
export function listAgentFileChanges(
  options?: { limit?: number; status?: string }
): Promise<AgentFileChangesPayload> {
  const params = new URLSearchParams();
  if (options?.limit != null) params.set("limit", String(options.limit));
  if (options?.status != null) params.set("status", options.status);
  const qs = params.toString();
  return apiFetch(
    `/api/files/changes${qs ? `?${qs}` : ""}`,
    parseAgentFileChangesPayload
  );
}

export function getAgentFileDiff(changeId: string): Promise<AgentFileDiffPayload> {
  return apiFetch(
    `/api/files/changes/${encodeURIComponent(changeId)}/diff`,
    parseAgentFileDiffPayload
  );
}

export function revertAgentFileChange(
  csrf: string,
  changeId: string
): Promise<AgentFileRevertPayload> {
  return apiFetch(
    `/api/files/changes/${encodeURIComponent(changeId)}/revert`,
    parseAgentFileRevertPayload,
    { method: "POST", csrf, body: "{}" }
  );
}

export function uploadFiles(
  csrf: string,
  path: string,
  files: File[],
  onProgress: (fileName: string, progress: number) => void
): Promise<UploadPayload> {
  const form = new FormData();
  for (const file of files) {
    form.append("file", file);
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/files/upload?path=${encodeURIComponent(path)}`);
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) {
        return;
      }
      const progress = Math.round((event.loaded / event.total) * 100);
      for (const file of files) {
        onProgress(file.name, progress);
      }
    };
    xhr.onload = () => {
      try {
        const payload: unknown = JSON.parse(xhr.responseText || "{}");
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(parseUploadPayload(payload));
          return;
        }
        reject(new Error(errorMessageFromPayload(payload) || UPLOAD_FAILED_LABEL));
      } catch (error) {
        reject(error instanceof Error ? error : new Error(UPLOAD_FAILED_LABEL));
      }
    };
    xhr.onerror = () => reject(new Error(UPLOAD_FAILED_LABEL));
    xhr.send(form);
  });
}
