import { MessageSquare } from "lucide-react";
import { NEW_CHAT_LABEL } from "../i18n/ru";
import type { ChatSummary } from "../types";

export type ChatListProps = {
  chats: ChatSummary[];
  activeChatId: number | null;
  onSelectChat: (chat: ChatSummary) => void;
  onNewChat: () => void;
  loading: boolean;
};

/**
 * Sidebar chat list. Etap 2A core: list + select + active highlight.
 * Rename/delete/time-range grouping are Etap 2B. A new-chat button is rendered
 * at the top so it stays pinned above the scroll area.
 */
export function ChatList({
  chats,
  activeChatId,
  onSelectChat,
  onNewChat,
  loading
}: ChatListProps) {
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
        <ul className="chat-list" role="list">
          {chats.map((chat) => {
            const isActive = chat.id === activeChatId;
            return (
              <li key={chat.id}>
                <button
                  className={`chat-item ${isActive ? "active" : ""} ${chat.active ? "is-active-session" : ""}`}
                  onClick={() => onSelectChat(chat)}
                  title={chat.title ?? `Чат #${chat.id}`}
                >
                  <span className="chat-item-title">
                    {chat.title ?? `Чат #${chat.id}`}
                  </span>
                  {chat.msg_count > 0 && (
                    <span className="chat-item-meta">{chat.msg_count}</span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
