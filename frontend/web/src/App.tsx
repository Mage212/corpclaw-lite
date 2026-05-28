import {
  Archive,
  Bot,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  File,
  FileImage,
  FileSpreadsheet,
  Folder,
  FolderPlus,
  Grid3X3,
  List,
  LogOut,
  Menu,
  MessageSquare,
  MoreVertical,
  MoveRight,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Search,
  Send,
  Trash2,
  Upload,
  X
} from "lucide-react";
import type { Dispatch, FormEvent, SetStateAction } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  copyFiles,
  deleteFiles,
  downloadUrl,
  getSession,
  listFiles,
  loadTree,
  login,
  logout,
  makeDirectory,
  moveFiles,
  previewFile,
  renameFile,
  searchFiles,
  uploadFiles
} from "./api";
import type {
  AgentMode,
  ApprovalRequest,
  ChatMessage,
  DirectoryPayload,
  FileEntry,
  PreviewPayload,
  SessionPayload,
  StatusLine,
  TreeNode,
  UploadItem,
  User,
  ViewMode
} from "./types";

const emptyStatus: StatusLine = {
  active: false,
  requestId: null,
  label: "",
  phase: "idle",
  tone: "idle"
};

function id(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2)}_${Date.now()}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parentPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function fileIcon(entry: FileEntry) {
  if (entry.is_dir) return <Folder size={18} />;
  if (entry.kind === "image") return <FileImage size={18} />;
  if (entry.kind === "spreadsheet") return <FileSpreadsheet size={18} />;
  if (entry.kind === "archive") return <Archive size={18} />;
  return <File size={18} />;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function App() {
  const [session, setSession] = useState<SessionPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getSession()
      .then(setSession)
      .catch(() => setSession({ authenticated: false, user: null, csrf_token: "" }))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <div className="boot">Загрузка CorpClaw Lite...</div>;
  }

  if (!session?.authenticated || !session.user) {
    return <LoginView onLogin={setSession} />;
  }

  return <Workspace session={session} onSessionChange={setSession} />;
}

function LoginView({ onLogin }: { onLogin: (session: SessionPayload) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onLogin(await login(username, password));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка входа");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="brand-mark">
          <Bot size={24} />
          <span>CorpClaw Lite</span>
        </div>
        <label>
          Логин
          <input
            id="username"
            name="username"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label>
          Пароль
          <input
            id="password"
            name="password"
            autoComplete="current-password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error && <div className="form-error">{error}</div>}
        <button className="primary" disabled={busy}>
          {busy ? "Проверяю..." : "Войти"}
        </button>
      </form>
    </main>
  );
}

function Workspace({
  session,
  onSessionChange
}: {
  session: SessionPayload;
  onSessionChange: (session: SessionPayload) => void;
}) {
  const [mode, setMode] = useState<AgentMode>("execute");
  const [filesOpen, setFilesOpen] = useState(true);
  const [preview, setPreview] = useState<PreviewPayload | null>(null);
  const user = session.user;

  if (!user) {
    return <LoginView onLogin={onSessionChange} />;
  }

  async function doLogout() {
    await logout(session.csrf_token);
    onSessionChange({ authenticated: false, user: null, csrf_token: "" });
  }

  return (
    <div className={`workspace ${filesOpen ? "with-files" : "files-hidden"}`}>
      <FileExplorer
        csrf={session.csrf_token}
        open={filesOpen}
        onPreview={setPreview}
        onToggle={() => setFilesOpen((value) => !value)}
      />
      <section className="main-pane">
        <header className="topbar">
          <button className="icon-button" onClick={() => setFilesOpen((value) => !value)}>
            {filesOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
          </button>
          <div className="topbar-title">
            <MessageSquare size={18} />
            <span>CorpClaw Lite</span>
          </div>
          <div className="topbar-actions">
            <SegmentedMode mode={mode} onModeChange={setMode} />
            <span className="user-pill">{user.name}</span>
            <button className="icon-button" onClick={doLogout} title="Выйти">
              <LogOut size={18} />
            </button>
          </div>
        </header>
        <ChatPanel csrf={session.csrf_token} mode={mode} user={user} />
      </section>
      {preview && <PreviewDrawer preview={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}

function SegmentedMode({
  mode,
  onModeChange
}: {
  mode: AgentMode;
  onModeChange: (mode: AgentMode) => void;
}) {
  return (
    <div className="segmented">
      <button className={mode === "execute" ? "active" : ""} onClick={() => onModeChange("execute")}>
        execute
      </button>
      <button className={mode === "chat" ? "active" : ""} onClick={() => onModeChange("chat")}>
        chat
      </button>
    </div>
  );
}

function ChatPanel({ csrf, mode, user }: { csrf: string; mode: AgentMode; user: User }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<StatusLine>(emptyStatus);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);

  const addMessage = useCallback((message: ChatMessage) => {
    setMessages((items) => [...items, message]);
  }, []);

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/chat?csrf=${encodeURIComponent(csrf)}`);
    wsRef.current = ws;
    ws.onopen = () => {
      setConnected(true);
      ws.send(JSON.stringify({ type: "mode_change", mode }));
    };
    ws.onclose = () => {
      setConnected(false);
      setStatus((current) =>
        current.active ? { ...current, tone: "warning", label: "Соединение с web-каналом закрыто" } : current
      );
    };
    ws.onmessage = (event) => {
      const parsed = JSON.parse(event.data) as unknown;
      if (!isRecord(parsed)) return;
      handleWsEvent(parsed, addMessage, setStatus, setApprovals);
    };
    return () => ws.close();
  }, [addMessage, csrf]);

  useEffect(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "mode_change", mode }));
    }
  }, [mode]);

  useEffect(() => {
    messagesRef.current?.scrollTo({ top: messagesRef.current.scrollHeight });
  }, [messages, status]);

  function send() {
    const text = input.trim();
    if (!text || wsRef.current?.readyState !== WebSocket.OPEN) return;
    addMessage({ id: id("msg"), role: "user", text });
    wsRef.current.send(JSON.stringify({ type: "message", message: text }));
    setInput("");
  }

  function answerApproval(approvalId: string, approved: boolean) {
    wsRef.current?.send(
      JSON.stringify({ type: approved ? "approve" : "deny", approval_id: approvalId })
    );
    setApprovals((items) => items.filter((item) => item.approval_id !== approvalId));
  }

  return (
    <main className="chat-shell">
      <div className="messages" ref={messagesRef}>
        {messages.length === 0 && (
          <div className="empty-chat">
            <Bot size={28} />
            <span>{user.name}, рабочая сессия готова.</span>
          </div>
        )}
        {messages.map((message) => (
          <article key={message.id} className={`message ${message.role} ${message.tone || "normal"}`}>
            {message.text}
          </article>
        ))}
        {approvals.map((approval) => (
          <div className="approval-card" key={approval.approval_id}>
            <strong>{approval.action}</strong>
            <p>{approval.details}</p>
            <div>
              <button className="primary" onClick={() => answerApproval(approval.approval_id, true)}>
                Разрешить
              </button>
              <button onClick={() => answerApproval(approval.approval_id, false)}>Отклонить</button>
            </div>
          </div>
        ))}
      </div>
      <StatusLineView status={status} connected={connected} />
      <footer className="composer">
        <textarea
          value={input}
          placeholder="Введите сообщение или задачу"
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              send();
            }
          }}
        />
        <button className="send-button" disabled={!connected || !input.trim()} onClick={send}>
          <Send size={18} />
        </button>
      </footer>
    </main>
  );
}

function handleWsEvent(
  event: Record<string, unknown>,
  addMessage: (message: ChatMessage) => void,
  setStatus: Dispatch<SetStateAction<StatusLine>>,
  setApprovals: Dispatch<SetStateAction<ApprovalRequest[]>>
) {
  const type = event.type;
  if (type === "request_started") {
    setStatus({
      active: true,
      requestId: String(event.request_id || ""),
      label: String(event.label || "В обработке..."),
      phase: "request",
      tone: "running"
    });
  } else if (type === "status_update") {
    setStatus({
      active: true,
      requestId: String(event.request_id || ""),
      label: String(event.label || event.key || "В обработке..."),
      phase: String(event.phase || "status"),
      tone: "running"
    });
  } else if (type === "status") {
    setStatus({
      active: true,
      requestId: null,
      label: `Статус: ${String(event.stage || "")}`,
      phase: "legacy",
      tone: "running"
    });
  } else if (type === "assistant_message") {
    addMessage({
      id: id("assistant"),
      role: "assistant",
      text: String(event.message || "")
    });
  } else if (type === "request_finished") {
    const tone = event.status === "error" ? "error" : event.status === "warning" ? "warning" : "done";
    setStatus({
      active: true,
      requestId: String(event.request_id || ""),
      label: String(event.label || "Готово"),
      phase: "done",
      tone
    });
    window.setTimeout(() => setStatus(emptyStatus), 1400);
  } else if (type === "warning") {
    addMessage({
      id: id("warning"),
      role: "system",
      text: String(event.message || "Предупреждение"),
      tone: "warning"
    });
    setStatus({
      active: true,
      requestId: String(event.request_id || ""),
      label: "Требуется внимание",
      phase: "warning",
      tone: "warning"
    });
  } else if (type === "error") {
    addMessage({
      id: id("error"),
      role: "system",
      text: String(event.message || "Ошибка"),
      tone: "error"
    });
  } else if (type === "file_ready") {
    const url = String(event.url || "");
    addMessage({
      id: id("file"),
      role: "system",
      text: `Файл готов: ${String(event.name || "download")} ${url}`,
      tone: "file"
    });
  } else if (type === "approval_required") {
    setApprovals((items) => [
      ...items,
      {
        approval_id: String(event.approval_id || ""),
        action: String(event.action || "Подтверждение"),
        details: String(event.details || "")
      }
    ]);
  }
}

function StatusLineView({ status, connected }: { status: StatusLine; connected: boolean }) {
  if (!status.active) {
    return <div className={`status-line ${connected ? "idle" : "warning"}`}>{connected ? "Готов" : "Нет соединения"}</div>;
  }
  return (
    <div className={`status-line ${status.tone}`}>
      <span className="pulse" />
      <span>{status.label}</span>
    </div>
  );
}

function FileExplorer({
  csrf,
  open,
  onPreview,
  onToggle
}: {
  csrf: string;
  open: boolean;
  onPreview: (preview: PreviewPayload) => void;
  onToggle: () => void;
}) {
  const [cwd, setCwd] = useState("");
  const [directory, setDirectory] = useState<DirectoryPayload>({ path: "", entries: [] });
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<FileEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [dropActive, setDropActive] = useState(false);
  const [context, setContext] = useState<{ x: number; y: number; entry: FileEntry } | null>(null);

  const entries = query.trim() ? searchResults : directory.entries;

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const [listing, loadedTree] = await Promise.all([listFiles(cwd), loadTree()]);
      setDirectory(listing);
      setTree(loadedTree);
    } finally {
      setBusy(false);
    }
  }, [cwd]);

  useEffect(() => {
    refresh().catch(console.error);
  }, [refresh]);

  useEffect(() => {
    const value = query.trim();
    if (!value) {
      setSearchResults([]);
      return;
    }
    const timer = window.setTimeout(() => {
      searchFiles(value)
        .then((payload) => setSearchResults(payload.entries))
        .catch(() => setSearchResults([]));
    }, 220);
    return () => window.clearTimeout(timer);
  }, [query]);

  async function upload(fileList: FileList | File[]) {
    const files = Array.from(fileList);
    if (!files.length) return;
    setUploads((items) => [
      ...items,
      ...files.map((file) => ({ id: id("upload"), name: file.name, progress: 0, status: "queued" as const }))
    ]);
    try {
      await uploadFiles(csrf, cwd, files, (fileName, progress) => {
        setUploads((items) =>
          items.map((item) =>
            item.name === fileName ? { ...item, progress, status: "uploading" } : item
          )
        );
      });
      setUploads((items) =>
        items.map((item) =>
          files.some((file) => file.name === item.name)
            ? { ...item, progress: 100, status: "done" }
            : item
        )
      );
      await refresh();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Upload failed";
      setUploads((items) =>
        items.map((item) =>
          files.some((file) => file.name === item.name)
            ? { ...item, status: "error", error: message }
            : item
        )
      );
    }
  }

  async function openEntry(entry: FileEntry) {
    if (entry.is_dir) {
      setCwd(entry.path);
      setSelected(new Set());
      return;
    }
    onPreview(await previewFile(entry.path));
  }

  function selectedPaths(entry?: FileEntry): string[] {
    if (entry && selected.size === 0) return [entry.path];
    if (entry && !selected.has(entry.path)) return [entry.path];
    return Array.from(selected);
  }

  async function doRename(entry: FileEntry) {
    const name = window.prompt("Новое имя", entry.name);
    if (!name || name === entry.name) return;
    await renameFile(csrf, entry.path, name);
    await refresh();
  }

  async function doMove(paths: string[]) {
    const target = window.prompt("Папка назначения", cwd);
    if (target === null) return;
    await moveFiles(csrf, paths, target);
    setSelected(new Set());
    await refresh();
  }

  async function doCopy(paths: string[]) {
    const target = window.prompt("Папка назначения", cwd);
    if (target === null) return;
    await copyFiles(csrf, paths, target);
    setSelected(new Set());
    await refresh();
  }

  async function doDelete(paths: string[]) {
    if (!paths.length) return;
    if (!window.confirm(`Удалить выбранные элементы: ${paths.length}?`)) return;
    await deleteFiles(csrf, paths, true);
    setSelected(new Set());
    await refresh();
  }

  async function doNewFolder() {
    const name = window.prompt("Имя папки");
    if (!name) return;
    await makeDirectory(csrf, cwd, name);
    await refresh();
  }

  function toggleSelected(entry: FileEntry, multi: boolean) {
    setSelected((current) => {
      const next = multi ? new Set(current) : new Set<string>();
      if (next.has(entry.path)) next.delete(entry.path);
      else next.add(entry.path);
      return next;
    });
  }

  async function handleDrop(event: React.DragEvent, targetDir?: string) {
    event.preventDefault();
    setDropActive(false);
    const dragged = event.dataTransfer.getData("application/x-corpclaw-paths");
    if (dragged && targetDir !== undefined) {
      const paths = JSON.parse(dragged) as string[];
      await moveFiles(csrf, paths, targetDir);
      setSelected(new Set());
      await refresh();
      return;
    }
    if (event.dataTransfer.files.length) {
      await upload(event.dataTransfer.files);
    }
  }

  if (!open) {
    return (
      <aside className="file-rail">
        <button className="icon-button" onClick={onToggle} title="Открыть файлы">
          <Menu size={18} />
        </button>
      </aside>
    );
  }

  return (
    <aside
      className={`file-explorer ${dropActive ? "drop-active" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        setDropActive(true);
      }}
      onDragLeave={() => setDropActive(false)}
      onDrop={(event) => handleDrop(event)}
    >
      <header className="files-header">
        <div>
          <strong>Workspace</strong>
          <span>{cwd || "root"}</span>
        </div>
        <button className="icon-button" onClick={onToggle}>
          <ChevronLeft size={18} />
        </button>
      </header>

      <div className="file-toolbar">
        <button className="icon-button" onClick={refresh} disabled={busy} title="Обновить">
          <RefreshCw size={17} />
        </button>
        <button className="icon-button" onClick={doNewFolder} title="Папка">
          <FolderPlus size={17} />
        </button>
        <label className="icon-button file-picker" title="Загрузить">
          <Upload size={17} />
          <input multiple type="file" onChange={(event) => event.target.files && upload(event.target.files)} />
        </label>
        <button
          className="icon-button"
          onClick={() => setViewMode(viewMode === "list" ? "grid" : "list")}
          title="Вид"
        >
          {viewMode === "list" ? <Grid3X3 size={17} /> : <List size={17} />}
        </button>
      </div>

      <div className="search-box">
        <Search size={15} />
        <input value={query} placeholder="Поиск файлов" onChange={(event) => setQuery(event.target.value)} />
      </div>

      <Breadcrumbs path={cwd} onNavigate={setCwd} />

      {tree && <TreeMini node={tree} current={cwd} onNavigate={setCwd} />}

      <div className="selection-actions">
        <span>{selected.size ? `Выбрано: ${selected.size}` : "Файлы"}</span>
        {selected.size > 0 && (
          <div>
            <button onClick={() => doCopy(Array.from(selected))}>
              <Copy size={14} /> Copy
            </button>
            <button onClick={() => doMove(Array.from(selected))}>
              <MoveRight size={14} /> Move
            </button>
            <button onClick={() => doDelete(Array.from(selected))}>
              <Trash2 size={14} /> Delete
            </button>
          </div>
        )}
      </div>

      <div className={`file-list ${viewMode}`}>
        {cwd && !query.trim() && (
          <button className="file-row up" onClick={() => setCwd(parentPath(cwd))}>
            <ChevronRight size={16} /> ..
          </button>
        )}
        {entries.map((entry) => (
          <div
            key={entry.path}
            className={`file-row ${selected.has(entry.path) ? "selected" : ""}`}
            draggable
            onClick={(event) => toggleSelected(entry, event.metaKey || event.ctrlKey)}
            onDoubleClick={() => openEntry(entry)}
            onContextMenu={(event) => {
              event.preventDefault();
              setContext({ x: event.clientX, y: event.clientY, entry });
            }}
            onDragStart={(event) => {
              const paths = selectedPaths(entry);
              event.dataTransfer.setData("application/x-corpclaw-paths", JSON.stringify(paths));
            }}
            onDragOver={(event) => entry.is_dir && event.preventDefault()}
            onDrop={(event) => entry.is_dir && handleDrop(event, entry.path)}
          >
            <span className="file-icon">{fileIcon(entry)}</span>
            <span className="file-name">{entry.name}</span>
            <span className="file-meta">{entry.is_dir ? "folder" : formatSize(entry.size_bytes)}</span>
            <span className="file-date">{entry.modified_at}</span>
            <button className="row-menu" onClick={() => setContext({ x: 0, y: 0, entry })}>
              <MoreVertical size={16} />
            </button>
          </div>
        ))}
      </div>

      <UploadQueue uploads={uploads} />

      {context && (
        <ContextMenu
          context={context}
          onClose={() => setContext(null)}
          onOpen={() => openEntry(context.entry)}
          onRename={() => doRename(context.entry)}
          onCopy={() => doCopy(selectedPaths(context.entry))}
          onMove={() => doMove(selectedPaths(context.entry))}
          onDelete={() => doDelete(selectedPaths(context.entry))}
        />
      )}
    </aside>
  );
}

function Breadcrumbs({ path, onNavigate }: { path: string; onNavigate: (path: string) => void }) {
  const parts = path.split("/").filter(Boolean);
  const crumbs = parts.map((part, index) => ({
    label: part,
    path: parts.slice(0, index + 1).join("/")
  }));
  return (
    <nav className="breadcrumbs">
      <button onClick={() => onNavigate("")}>root</button>
      {crumbs.map((crumb) => (
        <button key={crumb.path} onClick={() => onNavigate(crumb.path)}>
          / {crumb.label}
        </button>
      ))}
    </nav>
  );
}

function TreeMini({
  node,
  current,
  onNavigate
}: {
  node: TreeNode;
  current: string;
  onNavigate: (path: string) => void;
}) {
  const children = node.children || [];
  return (
    <div className="tree-mini">
      {children.slice(0, 12).map((child) => (
        <button
          key={child.path}
          className={child.path === current ? "active" : ""}
          onClick={() => onNavigate(child.path)}
        >
          <Folder size={14} /> {child.name}
        </button>
      ))}
    </div>
  );
}

function ContextMenu({
  context,
  onClose,
  onOpen,
  onRename,
  onCopy,
  onMove,
  onDelete
}: {
  context: { x: number; y: number; entry: FileEntry };
  onClose: () => void;
  onOpen: () => void;
  onRename: () => void;
  onCopy: () => void;
  onMove: () => void;
  onDelete: () => void;
}) {
  const style = context.x || context.y ? { left: context.x, top: context.y } : { right: 20, top: 168 };
  return (
    <div className="context-menu" style={style}>
      <button onClick={() => { onOpen(); onClose(); }}>{context.entry.is_dir ? "Открыть" : "Preview"}</button>
      {!context.entry.is_dir && (
        <a href={downloadUrl(context.entry.path)} onClick={onClose}>
          <Download size={14} /> Скачать
        </a>
      )}
      <button onClick={() => { onRename(); onClose(); }}>Переименовать</button>
      <button onClick={() => { onCopy(); onClose(); }}>Копировать</button>
      <button onClick={() => { onMove(); onClose(); }}>Переместить</button>
      <button className="danger" onClick={() => { onDelete(); onClose(); }}>Удалить</button>
      <button onClick={onClose}>Закрыть</button>
    </div>
  );
}

function UploadQueue({ uploads }: { uploads: UploadItem[] }) {
  if (!uploads.length) return null;
  return (
    <div className="upload-queue">
      {uploads.slice(-4).map((item) => (
        <div key={item.id} className={`upload-item ${item.status}`}>
          <span>{item.name}</span>
          <div>
            <i style={{ width: `${item.progress}%` }} />
          </div>
          {item.status === "error" && <small>{item.error}</small>}
        </div>
      ))}
    </div>
  );
}

function PreviewDrawer({ preview, onClose }: { preview: PreviewPayload; onClose: () => void }) {
  return (
    <aside className="preview-drawer">
      <header>
        <div>
          <strong>{preview.entry.name}</strong>
          <span>{preview.entry.path}</span>
        </div>
        <button className="icon-button" onClick={onClose}>
          <X size={18} />
        </button>
      </header>
      {preview.type === "image" && <img src={preview.url} alt={preview.entry.name} />}
      {preview.type === "text" && (
        <pre>{preview.error ? preview.error : preview.content}</pre>
      )}
      {preview.type === "metadata" && (
        <div className="metadata">
          <p>Тип: {preview.entry.kind}</p>
          <p>Размер: {formatSize(preview.entry.size_bytes)}</p>
          <p>Изменен: {preview.entry.modified_at}</p>
          <a className="primary link-button" href={downloadUrl(preview.entry.path)}>
            <Download size={16} /> Скачать
          </a>
        </div>
      )}
    </aside>
  );
}
