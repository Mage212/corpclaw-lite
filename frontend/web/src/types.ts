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
  | { type: "metadata"; entry: FileEntry }
  | { type: "empty" };

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

/** Ambient GPU/system load (DC-008 / D-088). Counts only — no personal data. */
export type SystemLoadLevel = "idle" | "busy" | "saturated";

export type SystemLoad = {
  active_count: number;
  max_concurrent: number;
  waiting_count: number;
  active_users: number;
  load_level: SystemLoadLevel;
  updated_at: number;
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

/**
 * Processing depth (Etap 3). Fast = no thinking, Think = reasoning on,
 * Research = force deep_research via the research subagent (Work-only).
 */
export type DepthMode = "fast" | "think" | "research";

/** A loaded extension as shown in the Extensions management view (Etap 4). */
export type ExtensionSummary = {
  id: string;
  name: string;
  description: string | null;
  version: string | null;
  status: string;
  type?: string;
  always?: boolean;
  keywords?: string[];
  capabilities?: string[];
  tools?: string[];
};

/** The full extensions payload from GET /api/extensions. */
export type ExtensionsPayload = {
  skills: ExtensionSummary[];
  subagents: ExtensionSummary[];
  mcp: ExtensionSummary[];
  plugins: ExtensionSummary[];
};

/** Agent context (personal instructions + tone) from GET/PUT /api/agent-context. */
export type AgentContextPayload = {
  instructions: string;
  tone: "default" | "concise" | "detailed";
};

/** Sidebar navigation section. Chat = conversational (tools off in Etap 2), Work = task (tools on). */
export type SidebarSection = "chat" | "work";

/**
 * Session section tag from the API. Includes durable system inbox (B-120 / DC-032),
 * which is not a Chat/Work tool-mode tab.
 */
export type ChatSection = SidebarSection | "system";

/** A chat session as shown in the sidebar chat list (from GET /api/chats). */
export type ChatSummary = {
  id: number;
  section: ChatSection;
  title: string | null;
  created_at: string;
  active: boolean;
  msg_count: number;
  /** Last-activity timestamp (drives time-range grouping). Null for legacy rows. */
  updated_at?: string | null;
  /** Folder grouping id (Etap 2B foundation — no UI grouping yet). */
  folder_id?: number | null;
  /**
   * B-090: agent run is in-flight for this session (in-memory gate).
   * Distinct from `active` (agent's write target) and viewed chat in UI.
   */
  is_running?: boolean;
};

/** B-140 / B-141: schedule task lifecycle status (consent-first). */
export type ScheduleTaskStatus = "pending" | "active" | "paused" | "done" | "dismissed";

export type ScheduleKind = "once" | "interval" | "cron" | "unset";

export type ScheduleSpec = {
  kind: ScheduleKind;
  run_at?: string | null;
  minutes?: number | null;
  expr?: string | null;
};

/** One scheduled agent task from GET /api/schedule (public payload, no claim fields). */
export type ScheduleTask = {
  id: string;
  user_id: number;
  title: string;
  task_text: string;
  schedule_text: string;
  schedule: ScheduleSpec;
  timezone: string;
  status: ScheduleTaskStatus;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  run_count: number;
  error_count: number;
  created_at: string;
  updated_at: string;
  accepted_at: string | null;
};

export type ScheduleAcceptOverrides = {
  title?: string;
  task_text?: string;
  schedule_text?: string;
};

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

/** B-094/B-095: file attachment metadata (pending inline or pin). */
export type ContextAttachment = {
  path: string;
  kind: string;
  tokens: number;
  approximate: boolean;
  mode: string;
  label: string;
};

export type PendingContextPayload = {
  session_id: number;
  pending_count: number;
  attachments: ContextAttachment[];
};

export type PinsPayload = {
  session_id: number;
  pins: ContextAttachment[];
  pin_tokens: number;
  pin_budget: number;
  pin_ratio: number;
  context_limit_tokens: number;
  pin?: ContextAttachment;
  reason?: string;
};

/** B-117: one agent file mutation from the change journal. */
export type AgentFileChange = {
  change_id: string;
  run_id: string;
  path: string;
  op: string;
  tool_name: string;
  status: string;
  size_bytes: number;
  created_at: number;
  has_backup: boolean;
};

export type AgentFileChangesPayload = {
  changes: AgentFileChange[];
};

export type AgentFileDiffPayload = {
  change_id: string;
  path: string;
  kind: string;
  unified_diff?: string;
  truncated?: boolean;
  before_hash?: string;
  after_hash?: string;
  message?: string;
};

export type AgentFileRevertPayload = {
  ok: boolean;
  change_id: string;
  action: string;
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
