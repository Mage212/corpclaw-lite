import {
  Activity,
  Bot,
  LogOut,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  RefreshCw,
  RotateCcw
} from "lucide-react";
import type { FormEvent } from "react";
import { useCallback, useEffect, useState } from "react";
import { getSession, getWorkspaceOverview, login, logout, previewFile } from "./api";
import { ChatPanel } from "./chat/ChatPanel";
import { useWebChatSession } from "./chat/useWebChatSession";
import { FileExplorer } from "./files/FileExplorer";
import { FilePreview } from "./files/FilePreview";
import { useResizablePanels } from "./hooks/useResizablePanels";
import { agentModeLabel } from "./i18n/ru";
import { InspectorPanel } from "./inspector/InspectorPanel";
import type {
  AgentMode,
  ContextUsage,
  FileExplorerMode,
  InspectorTab,
  PreviewMode,
  PreviewPayload,
  SessionPayload,
  WorkspaceOverviewPayload
} from "./types";

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
  const [filesMode, setFilesMode] = useState<FileExplorerMode>("side");
  const [preview, setPreview] = useState<PreviewPayload | null>(null);
  const [previewMode, setPreviewMode] = useState<PreviewMode>("side");
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("overview");
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null);
  const [overview, setOverview] = useState<WorkspaceOverviewPayload | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [resetSignal, setResetSignal] = useState(0);
  const { cssVars, prepareSidePreview, startResize } = useResizablePanels();
  const user = session.user;

  const refreshOverview = useCallback(() => {
    setOverviewLoading(true);
    getWorkspaceOverview()
      .then(setOverview)
      .catch((error) => console.warn("Failed to load workspace overview", error))
      .finally(() => setOverviewLoading(false));
  }, []);

  const chatSession = useWebChatSession({
    csrf: session.csrf_token,
    mode,
    resetSignal,
    onContextUsage: setContextUsage,
    onWorkspaceChanged: refreshOverview
  });

  useEffect(() => {
    refreshOverview();
  }, [refreshOverview]);

  if (!user) {
    return <LoginView onLogin={onSessionChange} />;
  }

  async function doLogout() {
    await logout(session.csrf_token);
    onSessionChange({ authenticated: false, user: null, csrf_token: "" });
  }

  function openPreview(next: PreviewPayload, nextMode: PreviewMode = "side") {
    if (nextMode === "side") {
      prepareSidePreview(filesOpen && filesMode === "side");
    }
    setPreview(next);
    setPreviewMode(nextMode);
    setInspectorTab("preview");
    setInspectorOpen(true);
  }

  async function openPreviewPath(path: string) {
    const next = await previewFile(path);
    openPreview(next, "side");
  }

  function toggleFiles() {
    setFilesOpen((value) => {
      if (value) {
        setFilesMode("side");
      }
      return !value;
    });
  }

  return (
    <div
      className={`workspace ${filesOpen ? "files-open" : "files-closed"} ${
        inspectorOpen ? "inspector-open" : "inspector-closed"
      } ${filesMode === "expanded" ? "files-expanded" : ""}`}
      style={cssVars}
    >
      <FileExplorer
        csrf={session.csrf_token}
        open={filesOpen}
        mode={filesMode}
        onModeChange={setFilesMode}
        onPreview={openPreview}
        onWorkspaceChanged={refreshOverview}
      />
      {filesOpen && filesMode === "side" && (
        <div
          className="resize-handle files-resize"
          onPointerDown={(event) =>
            startResize("files", event, {
              filesOpen: true,
              previewOpen: inspectorOpen
            })
          }
          role="separator"
          aria-orientation="vertical"
        />
      )}
      <section className="main-pane">
        <header className="topbar">
          <button className="icon-button topbar-files-toggle" onClick={toggleFiles} title="Файлы">
            {filesOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
          </button>
          <div className="topbar-center">
            <div className="topbar-title">
              <MessageSquare size={18} />
              <span>CorpClaw Lite</span>
            </div>
            <ContextMeter usage={contextUsage} />
          </div>
          <div className="topbar-mode">
            <SegmentedMode mode={mode} onModeChange={setMode} />
          </div>
          <div className="topbar-actions">
            <button
              className="icon-button"
              onClick={() => setInspectorOpen((value) => !value)}
              title={inspectorOpen ? "Скрыть операционный центр" : "Показать операционный центр"}
            >
              {inspectorOpen ? <PanelRightClose size={17} /> : <PanelRightOpen size={17} />}
            </button>
            <button
              className="icon-button"
              onClick={refreshOverview}
              disabled={overviewLoading}
              title="Обновить обзор"
            >
              <RefreshCw size={17} />
            </button>
            <button
              className="icon-button"
              onClick={() => setResetSignal((value) => value + 1)}
              title="Новая сессия"
            >
              <RotateCcw size={17} />
            </button>
            <span className="user-pill">{user.name}</span>
            <button className="icon-button" onClick={doLogout} title="Выйти">
              <LogOut size={18} />
            </button>
          </div>
        </header>
        <ChatPanel
          session={chatSession}
          user={user}
          onPreviewFile={openPreviewPath}
        />
      </section>
      {inspectorOpen && (
        <div
          className="resize-handle preview-resize"
          onPointerDown={(event) =>
            startResize("preview", event, {
              filesOpen: filesOpen && filesMode === "side",
              previewOpen: true
            })
          }
          role="separator"
          aria-orientation="vertical"
        />
      )}
      {inspectorOpen && (
        <InspectorPanel
          activeTab={inspectorTab}
          onTabChange={setInspectorTab}
          overview={overview}
          overviewLoading={overviewLoading}
          status={chatSession.status}
          runEvents={chatSession.runEvents}
          approvals={chatSession.approvals}
          contextUsage={contextUsage}
          preview={preview}
          previewMode={previewMode}
          onPreviewModeChange={setPreviewMode}
          onClose={() => setInspectorOpen(false)}
          onClosePreview={() => {
            setPreview(null);
            setInspectorTab("overview");
          }}
          onRefreshOverview={refreshOverview}
          onPreviewPath={openPreviewPath}
          onAnswerApproval={chatSession.answerApproval}
        />
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

function formatTokenCount(value: number): string {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)} тыс.`;
  }
  return String(value);
}

function ContextMeter({ usage }: { usage: ContextUsage | null }) {
  const latest = usage?.latest_total_tokens ?? 0;
  const limit = usage?.context_limit_tokens ?? 0;
  const ratio = usage?.context_ratio ?? 0;
  const tone = ratio >= 0.8 ? "danger" : ratio >= 0.6 ? "warning" : "normal";
  const percent = Math.round(ratio * 100);
  const label = limit ? `${formatTokenCount(latest)} / ${formatTokenCount(limit)}` : "—";

  return (
    <div className={`context-meter ${tone}`} title={`Контекст: ${percent}%`}>
      <Activity size={15} />
      <span>Контекст</span>
      <strong>{label}</strong>
      <i>
        <b style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
      </i>
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
        {agentModeLabel("execute")}
      </button>
      <button className={mode === "chat" ? "active" : ""} onClick={() => onModeChange("chat")}>
        {agentModeLabel("chat")}
      </button>
    </div>
  );
}
