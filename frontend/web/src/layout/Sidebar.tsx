import { useEffect, useRef, useState } from "react";
import { ChevronDown, LogOut, Settings2, Sparkles } from "lucide-react";
import {
  AGENT_CONTEXT_LABEL,
  COMING_SOON_LABEL,
  EXTENSIONS_LABEL,
  sidebarSectionLabel
} from "../i18n/ru";
import type { ChatSummary, SidebarSection, User } from "../types";
import { ChatList } from "./ChatList";

export type SidebarProps = {
  user: User;
  section: SidebarSection;
  onSectionChange: (section: SidebarSection) => void;
  chats: ChatSummary[];
  activeChatId: number | null;
  chatsLoading: boolean;
  onSelectChat: (chat: ChatSummary) => void;
  onNewChat: () => void;
  onRenameChat: (id: number, title: string) => void;
  onDeleteChat: (id: number) => void;
  onLogout: () => void;
};

export function Sidebar({
  user,
  section,
  onSectionChange,
  chats,
  activeChatId,
  chatsLoading,
  onSelectChat,
  onNewChat,
  onRenameChat,
  onDeleteChat,
  onLogout
}: SidebarProps) {
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const userMenuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!userMenuOpen) return;

    function onPointerDown(event: PointerEvent) {
      const node = userMenuRef.current;
      if (node && !node.contains(event.target as Node)) {
        setUserMenuOpen(false);
      }
    }

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setUserMenuOpen(false);
      }
    }

    window.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [userMenuOpen]);

  return (
    <aside className="sidebar" aria-label="Навигация">
      <SectionSwitcher value={section} onChange={onSectionChange} />

      <nav className="sidebar-management" aria-label="Управление агентом">
        <button disabled title={`${EXTENSIONS_LABEL} — ${COMING_SOON_LABEL}`}>
          <Settings2 size={16} />
          <span>{EXTENSIONS_LABEL}</span>
          <span className="coming-soon-tag">{COMING_SOON_LABEL}</span>
        </button>
        <button disabled title={`${AGENT_CONTEXT_LABEL} — ${COMING_SOON_LABEL}`}>
          <Sparkles size={16} />
          <span>{AGENT_CONTEXT_LABEL}</span>
          <span className="coming-soon-tag">{COMING_SOON_LABEL}</span>
        </button>
      </nav>

      <ChatList
        chats={chats}
        activeChatId={activeChatId}
        onSelectChat={onSelectChat}
        onNewChat={onNewChat}
        onRenameChat={onRenameChat}
        onDeleteChat={onDeleteChat}
        loading={chatsLoading}
      />

      <div className="sidebar-user-profile" ref={userMenuRef}>
        <div className="user-menu">
          <button
            className="user-pill"
            onClick={() => setUserMenuOpen((value) => !value)}
            aria-expanded={userMenuOpen}
            aria-haspopup="menu"
          >
            <span>{user.name}</span>
            <ChevronDown size={14} />
          </button>
          {userMenuOpen && (
            <div className="user-menu-popover" role="menu">
              <div className="user-menu-header">
                <strong>{user.name}</strong>
                <span>{user.department}</span>
              </div>
              <button
                className="user-menu-item danger"
                onClick={() => {
                  setUserMenuOpen(false);
                  onLogout();
                }}
                role="menuitem"
              >
                <LogOut size={16} />
                <span>Выйти</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}

function SectionSwitcher({
  value,
  onChange
}: {
  value: SidebarSection;
  onChange: (section: SidebarSection) => void;
}) {
  return (
    <div className="sidebar-section-switcher" role="tablist" aria-label="Раздел работы">
      {(["chat", "work"] as const).map((section) => (
        <button
          key={section}
          role="tab"
          aria-selected={value === section}
          className={value === section ? "active" : ""}
          onClick={() => onChange(section)}
        >
          {sidebarSectionLabel(section)}
        </button>
      ))}
    </div>
  );
}
