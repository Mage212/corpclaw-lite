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
  type: "request" | "llm" | "tool" | "approval" | "file" | "warning" | "error" | "done" | "reset";
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
export type InspectorTab = "overview" | "run" | "preview";

export type ContextUsage = {
  latest_total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  context_limit_tokens: number;
  context_ratio: number;
};

export type PanelLayoutState = {
  filesWidth: number;
  previewWidth: number;
};
