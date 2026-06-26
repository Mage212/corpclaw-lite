import { ChevronDown, ChevronUp, Eye, MessageSquare, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import type { FormEvent } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getSession, getWorkspaceOverview, login, logout, previewFile } from "./api";
import { ChatPanel } from "./chat/ChatPanel";
import { useWebChatSession } from "./chat/useWebChatSession";
import { FileExplorer } from "./files/FileExplorer";
import { useResizablePanels } from "./hooks/useResizablePanels";
import { BottomDrawer } from "./layout/BottomDrawer";
import { PreviewOverlay } from "./layout/PreviewOverlay";
import { Sidebar } from "./layout/Sidebar";
import type {
  AgentMode,
  ContextUsage,
  PreviewOverlayMode,
  PreviewPayload,
  SessionPayload,
  SidebarSection,
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
          <MessageSquare size={24} />
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
  // --- Agent mode is fixed for Etap 1A. The UI toggle is gone, but the value is
  // still required by useWebChatSession (it emits the WS `mode_change` event) and
  // by the backend (`tools_enabled = mode === "execute"`). Etap 2 will wire this to
  // the Chat/Work section selector. ---
  const [mode] = useState<AgentMode>("execute");
  const [section, setSection] = useState<SidebarSection>("chat");

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const [preview, setPreview] = useState<PreviewPayload | null>(null);
  const [previewMode, setPreviewMode] = useState<PreviewOverlayMode>("side");

  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null);
  const [overview, setOverview] = useState<WorkspaceOverviewPayload | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [resetSignal, setResetSignal] = useState(0);

  const { cssVars, layout, startResize, setDrawerHeight } = useResizablePanels();
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

  // Overview is unused in the current UI (the Inspector "Обзор" tab was removed in 1A),
  // but we keep refreshing it so the data is warm for future overview surfaces.
  // `overview`/`overviewLoading` are reserved for that.
  void overview;
  void overviewLoading;

  if (!user) {
    return <LoginView onLogin={onSessionChange} />;
  }

  async function doLogout() {
    await logout(session.csrf_token);
    onSessionChange({ authenticated: false, user: null, csrf_token: "" });
  }

  function startNewChat() {
    if (!window.confirm("Сбросить контекст и начать новую сессию?")) {
      return;
    }
    setResetSignal((value) => value + 1);
  }

  function openPreview(next: PreviewPayload, nextMode: PreviewOverlayMode = "side") {
    setPreview(next);
    setPreviewMode(nextMode);
  }

  async function openPreviewPath(path: string) {
    const next = await previewFile(path);
    openPreview(next, "side");
  }

  function toggleDrawer() {
    setDrawerOpen((open) => !open);
  }

  // Seed a sensible default drawer height the first time the drawer is opened
  // (when none is persisted). Subsequent open/close cycles reuse the persisted
  // height. Lives in an effect (not inside the setDrawerOpen updater) so the
  // state update stays pure and survives StrictMode double-invocation.
  useEffect(() => {
    if (drawerOpen && layout.drawerHeight === null) {
      setDrawerHeight(Math.round((window.innerHeight || 720) * 0.4));
    }
  }, [drawerOpen, layout.drawerHeight, setDrawerHeight]);

  const workspaceClass = useMemo(() => {
    return [
      "workspace",
      sidebarOpen ? "sidebar-open" : "",
      drawerOpen ? "drawer-open-root" : ""
    ]
      .filter(Boolean)
      .join(" ");
  }, [sidebarOpen, drawerOpen]);

  return (
    <div className={workspaceClass} style={cssVars} data-sidebar-open={sidebarOpen ? "1" : "0"}>
      <Sidebar
        user={user}
        section={section}
        onSectionChange={setSection}
        onNewChat={startNewChat}
        onLogout={doLogout}
      />

      <section className={`main-area ${drawerOpen ? "drawer-open" : ""}`}>
        <header className="topbar">
          <div className="topbar-actions topbar-leading">
            <button
              className="icon-button"
              onClick={() => setSidebarOpen((value) => !value)}
              title={sidebarOpen ? "Скрыть боковую панель" : "Показать боковую панель"}
            >
              {sidebarOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
            </button>
            <button
              className="icon-button"
              onClick={toggleDrawer}
              title="Файлы"
            >
              {drawerOpen ? <ChevronDown size={18} /> : <ChevronUp size={18} />}
            </button>
          </div>
          <div className="topbar-center">
            <div className="topbar-title">
              <MessageSquare size={18} />
              <span>CorpClaw Lite</span>
            </div>
          </div>
          <div className="topbar-actions topbar-trailing">
            <button
              className="icon-button"
              onClick={() =>
                preview
                  ? previewMode === "expanded"
                    ? setPreviewMode("side")
                    : setPreview(null)
                  : undefined
              }
              title="Просмотр"
              disabled={!preview}
            >
              <Eye size={18} />
            </button>
          </div>
        </header>

        <div className="main-pane">
          <ChatPanel
            session={chatSession}
            user={user}
            onPreviewFile={openPreviewPath}
            contextUsage={contextUsage}
          />
        </div>

        <BottomDrawer
          open={drawerOpen}
          onToggle={toggleDrawer}
          onStartResize={(event) => startResize("drawer", event)}
        >
          <FileExplorer
            csrf={session.csrf_token}
            open={drawerOpen}
            mode="side"
            onModeChange={() => undefined}
            onPreview={openPreview}
            onWorkspaceChanged={refreshOverview}
          />
        </BottomDrawer>
      </section>

      {preview && (
        <PreviewOverlay
          preview={preview}
          mode={previewMode}
          onModeChange={setPreviewMode}
          onClose={() => setPreview(null)}
          onStartResize={(event) => startResize("preview", event)}
        />
      )}
    </div>
  );
}
