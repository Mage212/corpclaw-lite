import type {
  AgentMode,
  ChatMessage,
  ChatSummary,
  ContextUsage,
  DepthMode,
  DirectoryPayload,
  ExtensionSummary,
  ExtensionsPayload,
  FileEntry,
  PanelLayoutState,
  PreviewPayload,
  SessionPayload,
  TreeNode,
  User,
  WebSocketTicketPayload,
  WorkspaceOutputSummary,
  WorkspaceOverviewPayload
} from "./types";

export type UploadPayload = {
  uploaded: { name: string; path: string }[];
};

export type ServerWsEvent =
  | { type: "chat_history"; messages: ChatMessage[]; has_more: boolean; session_id?: number; read_only?: boolean }
  | { type: "history_page"; messages: ChatMessage[]; has_more: boolean; session_id?: number }
  | { type: "chat_message"; message: ChatMessage }
  | { type: "request_started"; request_id: string; label: string; phase?: string; key?: string }
  | { type: "request_state"; request_id: string; label: string; phase?: string; key?: string }
  | { type: "status_update"; request_id: string; label: string; phase: string; key?: string }
  | { type: "status"; stage: string }
  | { type: "assistant_message"; message: string }
  | {
      type: "request_finished";
      request_id: string;
      status: "ok" | "warning" | "error";
      label: string;
      usage?: ContextUsage;
    }
  | { type: "context_usage"; usage: ContextUsage }
  | { type: "context_reset"; message: string; usage?: ContextUsage }
  | { type: "warning"; message: string; request_id?: string }
  | { type: "error"; message: string; request_id?: string; usage?: ContextUsage }
  | { type: "file_ready"; name: string; url: string; caption: string; path?: string | null }
  | { type: "approval_required"; approval_id: string; action: string; details: string; request_id?: string }
  | { type: "approval_resolved"; approval_id: string; request_id?: string }
  | { type: "llm_status"; status: string }
  | { type: "mode"; mode: AgentMode }
  | { type: "depth_mode"; depth_mode: DepthMode }
  | { type: "chat_renamed"; session_id: number; title: string }
  | { type: "chat_activated"; session_id: number; section: string; mode: AgentMode }
  | { type: "chat_list_changed" };

type JsonRecord = Record<string, unknown>;

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function errorMessageFromPayload(value: unknown): string | null {
  if (!isRecord(value)) return null;
  return typeof value.error === "string" ? value.error : null;
}

function invalid(label: string): Error {
  return new Error(`Некорректный ответ сервера: ${label}`);
}

function record(value: unknown, label: string): JsonRecord {
  if (!isRecord(value)) {
    throw invalid(label);
  }
  return value;
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

/**
 * Defense-in-depth: bound a backend/WS-provided URL to a safe scheme.
 *
 * Today the backend only ever produces same-origin relative URLs
 * (`/api/download/{token}`, `/api/files/inline?...`), so this is a safety net
 * against a future backend change or a compromised backend emitting
 * `javascript:`/`data:text/html` (which would be XSS in an `<a href>`).
 * Returns the URL unchanged when safe, `undefined` otherwise.
 */
function safeUrl(value: unknown): string | undefined {
  if (typeof value !== "string" || value === "") return undefined;
  // Relative (starts with `/`) or protocol-relative — same-origin, safe.
  if (value.startsWith("/") || value.startsWith("./")) return value;
  const lower = value.toLowerCase();
  if (lower.startsWith("http://") || lower.startsWith("https://")) return value;
  // Reject everything else (javascript:, data:, vbscript:, file:, …).
  return undefined;
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function requiredString(source: JsonRecord, key: string, label: string): string {
  const value = source[key];
  if (typeof value !== "string") {
    throw invalid(`${label}.${key}`);
  }
  return value;
}

function optionalNumber(value: unknown): number | undefined {
  const numeric =
    typeof value === "number"
      ? value
      : typeof value === "string" && value.trim()
        ? Number(value)
        : undefined;
  return numeric !== undefined && Number.isFinite(numeric) ? numeric : undefined;
}

function requiredNumber(source: JsonRecord, key: string, label: string): number {
  const value = optionalNumber(source[key]);
  if (value === undefined) {
    throw invalid(`${label}.${key}`);
  }
  return value;
}

function requiredBoolean(source: JsonRecord, key: string, label: string): boolean {
  const value = source[key];
  if (typeof value !== "boolean") {
    throw invalid(`${label}.${key}`);
  }
  return value;
}

function nullableString(source: JsonRecord, key: string, label: string): string | null {
  const value = source[key];
  if (value === null) return null;
  if (typeof value === "string") return value;
  throw invalid(`${label}.${key}`);
}

function parseArray<T>(
  value: unknown,
  parser: (item: unknown) => T,
  label: string
): T[] {
  if (!Array.isArray(value)) {
    throw invalid(label);
  }
  return value.map((item) => parser(item));
}

function parseStringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw invalid(label);
  }
  return value;
}

function clampRatio(value: number): number {
  return Math.max(0, Math.min(1, value));
}

export function parseUser(value: unknown): User {
  const source = record(value, "user");
  return {
    id: requiredNumber(source, "id", "user"),
    name: requiredString(source, "name", "user"),
    username: nullableString(source, "username", "user"),
    department: requiredString(source, "department", "user"),
    is_admin: requiredBoolean(source, "is_admin", "user")
  };
}

export function parseSessionPayload(value: unknown): SessionPayload {
  const source = record(value, "session");
  const authenticated = requiredBoolean(source, "authenticated", "session");
  const userValue = source.user;
  return {
    authenticated,
    user: userValue === null ? null : parseUser(userValue),
    csrf_token: requiredString(source, "csrf_token", "session")
  };
}

export function parseWebSocketTicketPayload(value: unknown): WebSocketTicketPayload {
  const source = record(value, "websocket ticket");
  return {
    ticket: requiredString(source, "ticket", "websocket ticket"),
    expires_in_seconds: requiredNumber(source, "expires_in_seconds", "websocket ticket")
  };
}

export function parseFileEntry(value: unknown): FileEntry {
  const source = record(value, "file entry");
  return {
    name: requiredString(source, "name", "file entry"),
    path: requiredString(source, "path", "file entry"),
    is_dir: requiredBoolean(source, "is_dir", "file entry"),
    size_bytes: requiredNumber(source, "size_bytes", "file entry"),
    modified_at: requiredString(source, "modified_at", "file entry"),
    kind: requiredString(source, "kind", "file entry"),
    extension: requiredString(source, "extension", "file entry"),
    mime_type: nullableString(source, "mime_type", "file entry"),
    protected: requiredBoolean(source, "protected", "file entry")
  };
}

/** Parse a chat summary object from GET /api/chats (or embedded in WS events). */
export function parseChatSummary(value: unknown): ChatSummary {
  const source = record(value, "chat summary");
  const rawSection = requiredString(source, "section", "chat summary");
  const titleValue = source.title;
  const updatedAt = optionalString(source.updated_at);
  const folderId = optionalNumber(source.folder_id);
  return {
    id: requiredNumber(source, "id", "chat summary"),
    section: rawSection === "work" ? "work" : "chat",
    title: typeof titleValue === "string" && titleValue.length > 0 ? titleValue : null,
    created_at: requiredString(source, "created_at", "chat summary"),
    active: requiredBoolean(source, "active", "chat summary"),
    msg_count: requiredNumber(source, "msg_count", "chat summary"),
    updated_at: updatedAt ?? null,
    folder_id: folderId ?? null
  };
}

export function parseChatSummaries(value: unknown): ChatSummary[] {
  if (!Array.isArray(value)) return [];
  return value.map(parseChatSummary);
}

// --- Etap 4: Extensions payload parsing ---

export function parseExtensionSummary(value: unknown): ExtensionSummary {
  const source = record(value, "extension summary");
  return {
    id: requiredString(source, "id", "extension summary"),
    name: requiredString(source, "name", "extension summary"),
    description: nullableString(source, "description", "extension summary"),
    version: nullableString(source, "version", "extension summary"),
    status: requiredString(source, "status", "extension summary"),
    ...(typeof source.type === "string" ? { type: source.type } : {}),
    ...(typeof source.always === "boolean" ? { always: source.always } : {}),
    ...(Array.isArray(source.keywords) ? { keywords: source.keywords as string[] } : {}),
    ...(Array.isArray(source.capabilities)
      ? { capabilities: source.capabilities as string[] }
      : {}),
    ...(Array.isArray(source.tools) ? { tools: source.tools as string[] } : {})
  };
}

export function parseExtensionsPayload(value: unknown): ExtensionsPayload {
  const source = record(value, "extensions payload");
  const parseList = (key: string): ExtensionSummary[] => {
    const arr = (source as Record<string, unknown>)[key];
    return Array.isArray(arr) ? arr.map(parseExtensionSummary) : [];
  };
  return {
    skills: parseList("skills"),
    subagents: parseList("subagents"),
    mcp: parseList("mcp"),
    plugins: parseList("plugins")
  };
}

function parseWorkspaceOutput(value: unknown): WorkspaceOutputSummary {
  const source = record(value, "workspace output");
  return {
    name: stringValue(source.name, "файл"),
    path: source.path === null ? null : optionalString(source.path) ?? null,
    url: source.url === null ? null : optionalString(source.url) ?? null,
    caption: stringValue(source.caption, ""),
    available: typeof source.available === "boolean" ? source.available : false,
    created_at: stringValue(source.created_at, "")
  };
}

export function parseWorkspaceOverviewPayload(value: unknown): WorkspaceOverviewPayload {
  const source = record(value, "workspace overview");
  const llm = record(source.llm, "workspace overview.llm");
  return {
    user: parseUser(source.user),
    llm: {
      provider: llm.provider === null ? null : optionalString(llm.provider) ?? null,
      model: llm.model === null ? null : optionalString(llm.model) ?? null
    },
    recent_files: parseArray(source.recent_files, parseFileEntry, "workspace overview.recent_files"),
    recent_outputs: parseArray(
      source.recent_outputs,
      parseWorkspaceOutput,
      "workspace overview.recent_outputs"
    )
  };
}

export function parseDirectoryPayload(value: unknown): DirectoryPayload {
  const source = record(value, "directory");
  return {
    path: requiredString(source, "path", "directory"),
    entries: parseArray(source.entries, parseFileEntry, "directory.entries")
  };
}

export function parseSearchPayload(value: unknown): { query: string; entries: FileEntry[] } {
  const source = record(value, "search payload");
  return {
    query: requiredString(source, "query", "search payload"),
    entries: parseArray(source.entries, parseFileEntry, "search payload.entries")
  };
}

export function parseTreeNode(value: unknown): TreeNode {
  const source = record(value, "tree node");
  const isDir = requiredBoolean(source, "is_dir", "tree node");
  const node: TreeNode = {
    name: requiredString(source, "name", "tree node"),
    path: requiredString(source, "path", "tree node"),
    is_dir: isDir,
    size_bytes: optionalNumber(source.size_bytes) ?? 0,
    modified_at: stringValue(source.modified_at, ""),
    kind: stringValue(source.kind, isDir ? "folder" : "file"),
    extension: stringValue(source.extension, ""),
    mime_type: source.mime_type === null ? null : optionalString(source.mime_type) ?? null,
    protected: typeof source.protected === "boolean" ? source.protected : false
  };
  if (source.children !== undefined) {
    node.children = parseArray(source.children, parseTreeNode, "tree node.children");
  }
  return node;
}

export function parsePreviewPayload(value: unknown): PreviewPayload {
  const source = record(value, "preview");
  const type = requiredString(source, "type", "preview");
  const entry = parseFileEntry(source.entry);
  if (type === "image") {
    const url = safeUrl(source.url);
    if (url === undefined) {
      throw invalid("preview.url");
    }
    return { type, entry, url };
  }
  if (type === "text") {
    const preview: PreviewPayload = {
      type,
      entry,
      truncated: requiredBoolean(source, "truncated", "preview"),
      content: requiredString(source, "content", "preview")
    };
    const error = optionalString(source.error);
    if (error !== undefined) {
      preview.error = error;
    }
    return preview;
  }
  if (type === "metadata") {
    return { type, entry };
  }
  throw invalid("preview.type");
}

export function parseOkPayload(value: unknown): { ok: boolean } {
  const source = record(value, "ok payload");
  return { ok: requiredBoolean(source, "ok", "ok payload") };
}

export function parsePathPayload(value: unknown): { path: string } {
  const source = record(value, "path payload");
  return { path: requiredString(source, "path", "path payload") };
}

export function parsePathsPayload(value: unknown): { paths: string[] } {
  const source = record(value, "paths payload");
  return { paths: parseStringArray(source.paths, "paths payload.paths") };
}

export function parseUploadPayload(value: unknown): UploadPayload {
  const source = record(value, "upload payload");
  return {
    uploaded: parseArray(
      source.uploaded,
      (item) => {
        const upload = record(item, "upload item");
        return {
          name: requiredString(upload, "name", "upload item"),
          path: requiredString(upload, "path", "upload item")
        };
      },
      "upload payload.uploaded"
    )
  };
}

export function parseContextUsage(value: unknown): ContextUsage | null {
  if (!isRecord(value)) return null;
  const latest = optionalNumber(value.latest_total_tokens) ?? 0;
  const limit = optionalNumber(value.context_limit_tokens) ?? 0;
  const fallbackRatio = limit > 0 ? latest / limit : 0;
  const ratio = optionalNumber(value.context_ratio) ?? fallbackRatio;
  return {
    latest_total_tokens: latest,
    input_tokens: optionalNumber(value.input_tokens) ?? 0,
    output_tokens: optionalNumber(value.output_tokens) ?? 0,
    total_tokens: optionalNumber(value.total_tokens) ?? 0,
    context_limit_tokens: limit,
    context_ratio: clampRatio(ratio)
  };
}

export function parseChatMessage(value: unknown): ChatMessage | null {
  if (!isRecord(value)) return null;
  const role = value.role;
  if (role !== "user" && role !== "assistant" && role !== "system") return null;
  const dbId = optionalNumber(value.db_id);
  const message: ChatMessage = {
    id: stringValue(value.id, dbId !== undefined ? `db_${dbId}` : `msg_${Date.now()}`),
    role,
    text: stringValue(value.text, "")
  };
  if (dbId !== undefined) {
    message.db_id = dbId;
  }
  const sessionId = optionalNumber(value.session_id);
  if (sessionId !== undefined) {
    message.session_id = sessionId;
  }
  const createdAt = optionalString(value.created_at);
  if (createdAt !== undefined) {
    message.created_at = createdAt;
  }
  const requestId = optionalString(value.request_id);
  if (requestId !== undefined) {
    message.request_id = requestId;
  }
  const tone = value.tone;
  if (tone === "warning" || tone === "error" || tone === "file" || tone === "normal") {
    message.tone = tone;
  }
  if (isRecord(value.file)) {
    const file: NonNullable<ChatMessage["file"]> = {
      name: stringValue(value.file.name, "файл")
    };
    const url = safeUrl(value.file.url);
    const path = value.file.path === null ? null : optionalString(value.file.path);
    const caption = optionalString(value.file.caption);
    const available = typeof value.file.available === "boolean" ? value.file.available : undefined;
    if (path !== undefined) {
      file.path = path;
    }
    if (url !== undefined) {
      file.url = url;
    }
    if (caption !== undefined) {
      file.caption = caption;
    }
    if (available !== undefined) {
      file.available = available;
    }
    message.file = file;
  }
  return message;
}

export function parseChatMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  return value.map(parseChatMessage).filter((item): item is ChatMessage => item !== null);
}

function requestStatus(value: unknown): "ok" | "warning" | "error" {
  return value === "warning" || value === "error" ? value : "ok";
}

function modeValue(value: unknown): AgentMode {
  return value === "chat" ? "chat" : "execute";
}

export function parseServerWsEvent(value: unknown): ServerWsEvent | null {
  if (!isRecord(value) || typeof value.type !== "string") return null;

  switch (value.type) {
    case "chat_history": {
      const event: Extract<ServerWsEvent, { type: "chat_history" }> = {
        type: "chat_history",
        messages: parseChatMessages(value.messages),
        has_more: value.has_more === true
      };
      const sessionId = optionalNumber(value.session_id);
      if (sessionId !== undefined) {
        event.session_id = sessionId;
      }
      if (value.read_only === true) {
        event.read_only = true;
      }
      return event;
    }
    case "history_page": {
      const event: Extract<ServerWsEvent, { type: "history_page" }> = {
        type: "history_page",
        messages: parseChatMessages(value.messages),
        has_more: value.has_more === true
      };
      const sessionId = optionalNumber(value.session_id);
      if (sessionId !== undefined) {
        event.session_id = sessionId;
      }
      return event;
    }
    case "chat_message": {
      const message = parseChatMessage(value.message);
      return message === null ? null : { type: "chat_message", message };
    }
    case "request_started": {
      const event: Extract<ServerWsEvent, { type: "request_started" }> = {
        type: "request_started",
        request_id: stringValue(value.request_id, ""),
        label: stringValue(value.label, "В обработке...")
      };
      const phase = optionalString(value.phase);
      const key = optionalString(value.key);
      if (phase !== undefined) event.phase = phase;
      if (key !== undefined) event.key = key;
      return event;
    }
    case "request_state": {
      const event: Extract<ServerWsEvent, { type: "request_state" }> = {
        type: "request_state",
        request_id: stringValue(value.request_id, ""),
        label: stringValue(value.label, "В обработке...")
      };
      const phase = optionalString(value.phase);
      const key = optionalString(value.key);
      if (phase !== undefined) event.phase = phase;
      if (key !== undefined) event.key = key;
      return event;
    }
    case "status_update": {
      const event: Extract<ServerWsEvent, { type: "status_update" }> = {
        type: "status_update",
        request_id: stringValue(value.request_id, ""),
        label: stringValue(value.label, stringValue(value.key, "В обработке...")),
        phase: stringValue(value.phase, "status")
      };
      const key = optionalString(value.key);
      if (key !== undefined) event.key = key;
      return event;
    }
    case "status":
      return { type: "status", stage: stringValue(value.stage, "") };
    case "assistant_message":
      return { type: "assistant_message", message: stringValue(value.message, "") };
    case "request_finished": {
      const event: Extract<ServerWsEvent, { type: "request_finished" }> = {
        type: "request_finished",
        request_id: stringValue(value.request_id, ""),
        status: requestStatus(value.status),
        label: stringValue(value.label, "Готово")
      };
      const usage = parseContextUsage(value.usage);
      if (usage !== null) {
        event.usage = usage;
      }
      return event;
    }
    case "context_usage": {
      const usage = parseContextUsage(value.usage);
      return usage === null ? null : { type: "context_usage", usage };
    }
    case "context_reset": {
      const event: Extract<ServerWsEvent, { type: "context_reset" }> = {
        type: "context_reset",
        message: stringValue(value.message, "Сессия сброшена")
      };
      const usage = parseContextUsage(value.usage);
      if (usage !== null) {
        event.usage = usage;
      }
      return event;
    }
    case "warning": {
      const event: Extract<ServerWsEvent, { type: "warning" }> = {
        type: "warning",
        message: stringValue(value.message, "Предупреждение")
      };
      const requestId = optionalString(value.request_id);
      if (requestId !== undefined) {
        event.request_id = requestId;
      }
      return event;
    }
    case "error": {
      const event: Extract<ServerWsEvent, { type: "error" }> = {
        type: "error",
        message: stringValue(value.message, "Ошибка")
      };
      const requestId = optionalString(value.request_id);
      if (requestId !== undefined) {
        event.request_id = requestId;
      }
      const usage = parseContextUsage(value.usage);
      if (usage !== null) {
        event.usage = usage;
      }
      return event;
    }
    case "file_ready": {
      const event: Extract<ServerWsEvent, { type: "file_ready" }> = {
        type: "file_ready",
        name: stringValue(value.name, "файл"),
        url: safeUrl(value.url) ?? "",
        caption: stringValue(value.caption, "")
      };
      if (value.path === null) {
        event.path = null;
      } else {
        const path = optionalString(value.path);
        if (path !== undefined) {
          event.path = path;
        }
      }
      return event;
    }
    case "approval_required": {
      const approvalRequired: Extract<
        ServerWsEvent,
        { type: "approval_required" }
      > = {
        type: "approval_required",
        approval_id: stringValue(value.approval_id, ""),
        action: stringValue(value.action, "Подтверждение"),
        details: stringValue(value.details, "")
      };
      // Defensive: the wire payload currently omits request_id (the orchestrator's
      // approval_cb has no request in scope), but a future backend fix may add it.
      // If present, prefer it over the client-side heuristic stamp.
      const approvalRequestId = optionalString(value.request_id);
      if (approvalRequestId !== undefined) {
        approvalRequired.request_id = approvalRequestId;
      }
      return approvalRequired;
    }
    case "approval_resolved": {
      const approvalResolved: Extract<
        ServerWsEvent,
        { type: "approval_resolved" }
      > = {
        type: "approval_resolved",
        approval_id: stringValue(value.approval_id, "")
      };
      const resolvedRequestId = optionalString(value.request_id);
      if (resolvedRequestId !== undefined) {
        approvalResolved.request_id = resolvedRequestId;
      }
      return approvalResolved;
    }
    case "llm_status":
      return { type: "llm_status", status: stringValue(value.status, "unknown") };
    case "mode":
      return { type: "mode", mode: modeValue(value.mode) };
    case "depth_mode": {
      const raw = value.depth_mode;
      return {
        type: "depth_mode",
        depth_mode: raw === "fast" ? "fast" : raw === "research" ? "research" : "think"
      };
    }
    case "chat_renamed": {
      const renamedId = optionalNumber(value.session_id);
      if (renamedId === undefined) return null;
      return {
        type: "chat_renamed",
        session_id: renamedId,
        title: stringValue(value.title, "")
      };
    }
    case "chat_activated": {
      const activatedId = optionalNumber(value.session_id);
      if (activatedId === undefined) return null;
      const sectionValue = stringValue(value.section, "chat");
      return {
        type: "chat_activated",
        session_id: activatedId,
        section: sectionValue === "work" ? "work" : "chat",
        mode: modeValue(value.mode)
      };
    }
    case "chat_list_changed":
      return { type: "chat_list_changed" };
    default:
      return null;
  }
}

export function parseDraggedPaths(value: string): string[] {
  try {
    const parsed: unknown = JSON.parse(value);
    return parseStringArray(parsed, "dragged paths");
  } catch {
    return [];
  }
}

export function parsePanelLayoutState(value: unknown): PanelLayoutState | null {
  if (!isRecord(value)) return null;

  // Etap 1A back-compat: legacy layout stored `filesWidth`; migrate to `sidebarWidth`.
  // New writes use `sidebarWidth`; both are accepted so existing users don't lose their width.
  const sidebarWidth =
    optionalNumber(value.sidebarWidth) ?? optionalNumber(value.filesWidth);
  const previewWidth = optionalNumber(value.previewWidth);
  if (sidebarWidth === undefined || previewWidth === undefined) {
    return null;
  }
  // `drawerHeight` is optional: absent/null → drawer collapsed (peek-bar only).
  const rawDrawerHeight = optionalNumber(value.drawerHeight);
  const drawerHeight =
    rawDrawerHeight === undefined || rawDrawerHeight <= 0 ? null : rawDrawerHeight;
  return { sidebarWidth, previewWidth, drawerHeight };
}
