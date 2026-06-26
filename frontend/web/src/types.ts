export type User = {
  id: number;
  name: string;
  username: string | null;
  department: string;
  is_admin: boolean;
};

export type SessionPayload = {
  authenticated: boolean;
  user: User | null;
  csrf_token: string;
};

export type WebSocketTicketPayload = {
  ticket: string;
  expires_in_seconds: number;
};

export type FileEntry = {
  name: string;
  path: string;
  is_dir: boolean;
  size_bytes: number;
  modified_at: string;
  kind: string;
  extension: string;
  mime_type: string | null;
  protected: boolean;
};

export type DirectoryPayload = {
  path: string;
  entries: FileEntry[];
};

export type TreeNode = FileEntry & {
  children?: TreeNode[];
};

export type PreviewPayload =
  | { type: "image"; entry: FileEntry; url: string }
  | { type: "text"; entry: FileEntry; truncated: boolean; content: string; error?: string }
  | { type: "metadata"; entry: FileEntry };

export type ChatMessage = {
  id: string;
  db_id?: number;
  session_id?: number;
  role: "user" | "assistant" | "system";
  text: string;
  created_at?: string;
  request_id?: string;
  tone?: "normal" | "warning" | "error" | "file";
  file?: {
    name: string;
    path?: string | null;
    url?: string;
    caption?: string;
    available?: boolean;
  };
};

export type StatusLine = {
  active: boolean;
  requestId: string | null;
  label: string;
  phase: string;
  tone: "idle" | "running" | "warning" | "error" | "done";
};

export type ApprovalRequest = {
  approval_id: string;
  action: string;
  details: string;
  /**
   * Request this approval belongs to. Not carried on the wire (the WS
   * `approval_required` event has no request_id) — stamped client-side in
   * `useWebChatSession` via `lastActiveRequestIdRef` so approvals can group
   * inside their request's ActivityCard. `null` when no request is active.
   */
  request_id?: string | null;
};

export type WorkspaceOutputSummary = {
  name: string;
  path: string | null;
  url: string | null;
  caption: string;
  available: boolean;
  created_at: string;
};

export type WorkspaceOverviewPayload = {
  user: User;
  llm: {
    provider: string | null;
    model: string | null;
  };
  recent_files: FileEntry[];
  recent_outputs: WorkspaceOutputSummary[];
};

export type RunTimelineEvent = {
  id: string;
  requestId: string | null;
  type:
    | "request"
    | "queue"
    | "llm"
    | "tool"
    | "subagent"
    | "approval"
    | "file"
    | "warning"
    | "error"
    | "done"
    | "reset";
  label: string;
  detail?: string | undefined;
  tone: "idle" | "running" | "warning" | "error" | "done";
  createdAt: string;
};

export type UploadItem = {
  id: string;
  name: string;
  progress: number;
  status: "queued" | "uploading" | "done" | "error";
  error?: string;
};

export type ViewMode = "list" | "grid" | "details";
export type AgentMode = "execute" | "chat";
export type PreviewMode = "side" | "expanded";
export type FileExplorerMode = "side" | "expanded";

/** Sidebar navigation section. Chat = conversational (tools off in Etap 2), Work = task (tools on). */
export type SidebarSection = "chat" | "work";

/** Where the preview overlay renders: slide-in panel on the right, or fullscreen modal. */
export type PreviewOverlayMode = "side" | "expanded";

export type ContextUsage = {
  latest_total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  context_limit_tokens: number;
  context_ratio: number;
};

/**
 * Persisted workspace layout dimensions.
 *
 * - `sidebarWidth` — left navigation sidebar width (px). Was `filesWidth` pre-Etap 1A.
 * - `previewWidth` — preview overlay width when in `side` mode (px).
 * - `drawerHeight` — bottom file-drawer height (px). `null` = collapsed (peek-bar only).
 *
 * Stored under localStorage key `corpclaw.web.panelLayout`.
 */
export type PanelLayoutState = {
  sidebarWidth: number;
  previewWidth: number;
  drawerHeight: number | null;
};
