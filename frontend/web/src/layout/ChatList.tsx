import { MessageSquare, MoreVertical, Pencil, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { NEW_CHAT_LABEL } from "../i18n/ru";
import type { ChatSummary } from "../types";

export type ChatListProps = {
  chats: ChatSummary[];
  activeChatId: number | null;
  onSelectChat: (chat: ChatSummary) => void;
  onNewChat: () => void;
  onRenameChat: (id: number, title: string) => void;
  onDeleteChat: (id: number) => void;
  loading: boolean;
};

/**
 * Sidebar chat list. Etap 2B: list + select + rename-inline + delete +
 * time-range grouping (Сегодня / Вчера / Предыдущие 7 дней / Ранее).
 * Folders are a foundation only (folder_id column); UI grouping is future.
 */
export function ChatList({
  chats,
  activeChatId,
  onSelectChat,
  onNewChat,
  onRenameChat,
  onDeleteChat,
  loading
}: ChatListProps) {
  const groups = groupByTimeRange(chats);

  return (
    <div className="sidebar-chats">
      <button className="new-chat-btn" onClick={onNewChat} title={NEW_CHAT_LABEL}>
        <MessageSquare size={16} />
        <span>{NEW_CHAT_LABEL}</span>
      </button>
      {loading ? (
        <div className="sidebar-chats-placeholder">Загрузка чатов…</div>
      ) : chats.length === 0 ? (
        <div className="sidebar-chats-placeholder">Нет чатов в этом разделе.</div>
      ) : (
        groups.map((group) => (
          <div className="chat-group" key={group.key}>
            <div className="chat-group-header">{group.label}</div>
            <ul className="chat-list" role="list">
              {group.chats.map((chat) => (
                <ChatRow
                  key={chat.id}
                  chat={chat}
                  isActiveViewed={chat.id === activeChatId}
                  onSelect={() => onSelectChat(chat)}
                  onRename={(title) => onRenameChat(chat.id, title)}
                  onDelete={() => onDeleteChat(chat.id)}
                />
              ))}
            </ul>
          </div>
        ))
      )}
    </div>
  );
}

function ChatRow({
  chat,
  isActiveViewed,
  onSelect,
  onRename,
  onDelete
}: {
  chat: ChatSummary;
  isActiveViewed: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(chat.title ?? "");
  const menuRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  useEffect(() => {
    if (!menuOpen) return;
    function onPointerDown(event: PointerEvent) {
      const node = menuRef.current;
      if (node && !node.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }
    window.addEventListener("pointerdown", onPointerDown);
    return () => window.removeEventListener("pointerdown", onPointerDown);
  }, [menuOpen]);

  function commitRename() {
    const trimmed = draft.trim();
    if (trimmed && trimmed !== chat.title) {
      onRename(trimmed.slice(0, 200));
    }
    setEditing(false);
  }

  function startRename() {
    setMenuOpen(false);
    setDraft(chat.title ?? "");
    setEditing(true);
  }

  function requestDelete() {
    setMenuOpen(false);
    if (chat.active) return; // cannot delete active session
    const label = chat.title ?? `Чат #${chat.id}`;
    if (window.confirm(`Удалить чат «${label}»?`)) {
      onDelete();
    }
  }

  if (editing) {
    return (
      <li>
        <input
          ref={inputRef}
          className="chat-item-edit-input"
          value={draft}
          maxLength={200}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commitRename}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commitRename();
            } else if (event.key === "Escape") {
              event.preventDefault();
              setEditing(false);
            }
          }}
        />
      </li>
    );
  }

  return (
    <li>
      <div
        className={`chat-item ${isActiveViewed ? "active" : ""} ${chat.active ? "is-active-session" : ""}`}
      >
        <button className="chat-item-main" onClick={onSelect} title={chat.title ?? `Чат #${chat.id}`}>
          <span className="chat-item-title">{chat.title ?? `Чат #${chat.id}`}</span>
          {chat.msg_count > 0 && <span className="chat-item-meta">{chat.msg_count}</span>}
        </button>
        <div className="chat-item-menu" ref={menuRef}>
          <button
            className="chat-item-menu-toggle"
            onClick={() => setMenuOpen((value) => !value)}
            title="Действия"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
          >
            <MoreVertical size={15} />
          </button>
          {menuOpen && (
            <div className="chat-item-menu-popover" role="menu">
              <button className="chat-item-menu-item" onClick={startRename} role="menuitem">
                <Pencil size={14} />
                <span>Переименовать</span>
              </button>
              <button
                className="chat-item-menu-item danger"
                onClick={requestDelete}
                role="menuitem"
                disabled={chat.active}
                title={chat.active ? "Нельзя удалить активный чат" : "Удалить"}
              >
                <Trash2 size={14} />
                <span>Удалить</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

// --- time-range grouping (FE-side, by updated_at with created_at fallback) ---

type ChatGroup = { key: string; label: string; chats: ChatSummary[] };

function groupByTimeRange(chats: ChatSummary[]): ChatGroup[] {
  // Chats arrive already sorted (active first, then updated_at DESC). We keep
  // relative order within each bucket; the active chat stays first regardless.
  const today: ChatSummary[] = [];
  const yesterday: ChatSummary[] = [];
  const week: ChatSummary[] = [];
  const earlier: ChatSummary[] = [];

  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000;
  const startOfWeek = startOfToday - 7 * 24 * 60 * 60 * 1000;

  for (const chat of chats) {
    const ts = parseTimestamp(chat.updated_at) ?? parseTimestamp(chat.created_at) ?? 0;
    if (ts >= startOfToday) today.push(chat);
    else if (ts >= startOfYesterday) yesterday.push(chat);
    else if (ts >= startOfWeek) week.push(chat);
    else earlier.push(chat);
  }

  return [
    { key: "today", label: "Сегодня", chats: today },
    { key: "yesterday", label: "Вчера", chats: yesterday },
    { key: "week", label: "Предыдущие 7 дней", chats: week },
    { key: "earlier", label: "Ранее", chats: earlier }
  ].filter((group) => group.chats.length > 0);
}

/** Parse backend timestamps (SQLite DATETIME: "YYYY-MM-DD HH:MM:SS" or ISO). */
function parseTimestamp(value: string | null | undefined): number | null {
  if (!value) return null;
  // SQLite CURRENT_TIMESTAMP uses a space separator; Date.parse needs "T".
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  const ts = Date.parse(normalized);
  return Number.isNaN(ts) ? null : ts;
}
