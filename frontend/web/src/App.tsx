import {
  Bot,
  LogOut,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen
} from "lucide-react";
import type { FormEvent } from "react";
import { useEffect, useState } from "react";
import { getSession, login, logout } from "./api";
import { ChatPanel } from "./chat/ChatPanel";
import { FileExplorer } from "./files/FileExplorer";
import { FilePreview } from "./files/FilePreview";
import { useResizablePanels } from "./hooks/useResizablePanels";
import type { AgentMode, PreviewMode, PreviewPayload, SessionPayload } from "./types";

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
  const [previewMode, setPreviewMode] = useState<PreviewMode>("side");
  const { cssVars, startResize } = useResizablePanels();
  const user = session.user;

  if (!user) {
    return <LoginView onLogin={onSessionChange} />;
  }

  async function doLogout() {
    await logout(session.csrf_token);
    onSessionChange({ authenticated: false, user: null, csrf_token: "" });
  }

  function openPreview(next: PreviewPayload) {
    setPreview(next);
    setPreviewMode("side");
  }

  return (
    <div
      className={`workspace ${filesOpen ? "files-open" : "files-closed"} ${
        preview && previewMode === "side" ? "preview-open" : ""
      }`}
      style={cssVars}
    >
      <FileExplorer
        csrf={session.csrf_token}
        open={filesOpen}
        onPreview={openPreview}
      />
      {filesOpen && (
        <div
          className="resize-handle files-resize"
          onPointerDown={(event) => startResize("files", event)}
          role="separator"
          aria-orientation="vertical"
        />
      )}
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
      {preview && previewMode === "side" && (
        <>
          <div
            className="resize-handle preview-resize"
            onPointerDown={(event) => startResize("preview", event)}
            role="separator"
            aria-orientation="vertical"
          />
          <FilePreview
            preview={preview}
            mode={previewMode}
            onModeChange={setPreviewMode}
            onClose={() => setPreview(null)}
          />
        </>
      )}
      {preview && previewMode === "expanded" && (
        <FilePreview
          preview={preview}
          mode={previewMode}
          onModeChange={setPreviewMode}
          onClose={() => setPreview(null)}
        />
      )}
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
