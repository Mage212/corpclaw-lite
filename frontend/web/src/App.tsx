import { ChevronDown, ChevronUp, Eye, MessageSquare, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import type { FormEvent } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  activateChat,
  createChat,
  deleteChat as apiDeleteChat,
  getChats,
  getExtensions,
  getSession,
  login,
  logout,
  previewFile,
  reloadExtensions,
  renameChat as apiRenameChat
} from "./api";
import { ChatPanel } from "./chat/ChatPanel";
import { useWebChatSession } from "./chat/useWebChatSession";
import { FileExplorer } from "./files/FileExplorer";
import { useResizablePanels } from "./hooks/useResizablePanels";
import { BottomDrawer } from "./layout/BottomDrawer";
import { AgentContextView } from "./layout/AgentContextView";
import { ExtensionsView } from "./layout/ExtensionsView";
import { PreviewOverlay } from "./layout/PreviewOverlay";
import { Sidebar } from "./layout/Sidebar";
import type {
  ChatSummary,
  ContextUsage,
  DepthMode,
  ExtensionsPayload,
  PreviewOverlayMode,
  PreviewPayload,
  SessionPayload,
  SidebarSection
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
  // --- Mode is derived server-side from the active chat's section. The FE no
  // longer sends mode_change (vestigial after Etap 2 — the hook still accepts
  // it for back-compat with older builds, but we pass a constant default). ---
  const [section, setSection] = useState<SidebarSection>("chat");
  // Etap 3: depth mode (Fast/Think) — orthogonal to section (tools on/off).
  const [depthMode, setDepthMode] = useState<DepthMode>("think");

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const [preview, setPreview] = useState<PreviewPayload | null>(null);
  const [previewMode, setPreviewMode] = useState<PreviewOverlayMode>("side");

  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null);
  const [resetSignal, setResetSignal] = useState(0);

  // Etap 2: multi-chat. chatId=null = follow the active chat (loaded on connect).
  const [chatId, setChatId] = useState<number | null>(null);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [chatsLoading, setChatsLoading] = useState(false);

  // Etap 4/5: view state ("chat" | "extensions" | "agent-context").
  const [view, setView] = useState<"chat" | "extensions" | "agent-context">("chat");
  const [extensions, setExtensions] = useState<ExtensionsPayload | null>(null);
  const [extensionsLoading, setExtensionsLoading] = useState(false);

  const { cssVars, layout, startResize, setDrawerHeight } = useResizablePanels();
  const user = session.user;

  const refreshChats = useCallback(() => {
    setChatsLoading(true);
    getChats(session.csrf_token, section)
      .then(setChats)
      .catch((error) => console.warn("Failed to load chats", error))
      .finally(() => setChatsLoading(false));
  }, [session.csrf_token, section]);

  // Etap 4: extensions list (loaded on demand when view opens or reload clicked).
  const refreshExtensions = useCallback(() => {
    setExtensionsLoading(true);
    getExtensions()
      .then(setExtensions)
      .catch((error) => console.warn("Failed to load extensions", error))
      .finally(() => setExtensionsLoading(false));
  }, []);

  const handleOpenExtensions = useCallback(() => {
    setView("extensions");
    refreshExtensions();
  }, [refreshExtensions]);

  const handleReloadExtensions = useCallback(() => {
    reloadExtensions(session.csrf_token)
      .then(() => refreshExtensions())
      .catch((error) => console.warn("Failed to reload extensions", error));
  }, [session.csrf_token, refreshExtensions]);

  const handleActivateViewedChat = useCallback(
    async (targetChatId: number): Promise<boolean> => {
      try {
        await activateChat(session.csrf_token, targetChatId);
        return true;
      } catch (error) {
        console.warn("Failed to activate viewed chat", error);
        return false;
      }
    },
    [session.csrf_token]
  );

  const chatSession = useWebChatSession({
    csrf: session.csrf_token,
    depthMode,
    resetSignal,
    onContextUsage: setContextUsage,
    chatId,
    onActivateViewedChat: handleActivateViewedChat,
    onChatActivated: () => setChatId(null),
    onChatRenamed: refreshChats,
    onChatListChanged: refreshChats
  });

  useEffect(() => {
    refreshChats();
  }, [refreshChats]);

  // Etap 3B: Research mode requires tools (dispatch_subagent), which are off in
  // the Chat section. If the user switches to Chat while Research is selected,
  // fall back to Think so the depth stays meaningful.
  useEffect(() => {
    if (section === "chat" && depthMode === "research") {
      setDepthMode("think");
    }
  }, [section, depthMode]);

  if (!user) {
    return <LoginView onLogin={onSessionChange} />;
  }

  function selectChat(chat: ChatSummary) {
    // Load the chat's transcript. The active chat loads as editable
    // (read_only=false on the server, since it's the chat the agent writes to);
    // any other chat loads read-only until the user sends (activate-on-send).
    // Previously clicking the active chat did setChatId(null) ("follow"), but
    // null means "no chat viewed → empty panel", which made returning to the
    // active chat's transcript impossible after viewing another one.
    setChatId(chat.id);
  }

  async function startNewChat() {
    setChatsLoading(true);
    try {
      const created = await createChat(session.csrf_token, section);
      // New chat is active server-side; follow it (chatId=null loads the active one).
      setChatId(null);
      setChats((current) => [created, ...current.filter((chat) => chat.id !== created.id)]);
    } catch (error) {
      console.warn("Failed to create chat", error);
    } finally {
      setChatsLoading(false);
    }
  }

  async function renameChat(id: number, title: string) {
    try {
      await apiRenameChat(session.csrf_token, id, title);
      refreshChats();
    } catch (error) {
      console.warn("Failed to rename chat", error);
    }
  }

  async function deleteChat(id: number) {
    try {
      await apiDeleteChat(session.csrf_token, id);
      // If the deleted chat was being viewed, fall back to the active one.
      if (chatId === id) {
        setChatId(null);
      }
      refreshChats();
    } catch (error) {
      console.warn("Failed to delete chat", error);
    }
  }

  async function doLogout() {
    await logout(session.csrf_token);
    onSessionChange({ authenticated: false, user: null, csrf_token: "" });
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
        chats={chats}
        activeChatId={chatId}
        chatsLoading={chatsLoading}
        onSelectChat={selectChat}
        onNewChat={startNewChat}
        onRenameChat={renameChat}
        onDeleteChat={deleteChat}
        onLogout={doLogout}
        onOpenExtensions={handleOpenExtensions}
        onOpenAgentContext={() => setView("agent-context")}
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
              onClick={() => {
                if (!preview) {
                  // No file open — open the overlay with an empty state so the
                  // user sees where previews will appear (and that the panel works).
                  setPreview({ type: "empty" });
                } else if (previewMode === "expanded") {
                  setPreviewMode("side");
                } else {
                  setPreview(null);
                }
              }}
              title="Просмотр"
            >
              <Eye size={18} />
            </button>
          </div>
        </header>

        <div className="main-pane">
          <div className={`view-pane ${view === "chat" ? "" : "view-hidden"}`}>
            <ChatPanel
              session={chatSession}
              user={user}
              onPreviewFile={openPreviewPath}
              contextUsage={contextUsage}
              depthMode={depthMode}
              onDepthModeChange={setDepthMode}
              section={section}
            />
          </div>
          {view === "extensions" && (
            <ExtensionsView
              extensions={extensions}
              loading={extensionsLoading}
              onReload={handleReloadExtensions}
              onBack={() => setView("chat")}
            />
          )}
          {view === "agent-context" && (
            <AgentContextView
              csrf={session.csrf_token}
              onBack={() => setView("chat")}
            />
          )}
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
