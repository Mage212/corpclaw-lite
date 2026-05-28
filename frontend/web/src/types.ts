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
  role: "user" | "assistant" | "system";
  text: string;
  tone?: "normal" | "warning" | "error" | "file";
  file?: {
    name: string;
    url: string;
    caption?: string;
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
