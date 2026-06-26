import { Bot, CheckCircle2, CircleAlert, Download, Eye, Send, Sparkles } from "lucide-react";
import { useLayoutEffect, useRef } from "react";
import type { ChatMessage, ContextUsage, StatusLine, User } from "../types";
import { ActivityCard } from "./ActivityCard";
import { ContextSizeBar } from "./ContextSizeBar";
import { MarkdownMessage } from "./MarkdownMessage";
import type { WebChatSession } from "./useWebChatSession";

type ChatPanelProps = {
  session: WebChatSession;
  user: User;
  onPreviewFile: (path: string) => void;
  contextUsage: ContextUsage | null;
};

export function ChatPanel({ session, user, onPreviewFile, contextUsage }: ChatPanelProps) {
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const preserveScrollRef = useRef<{ height: number; top: number } | null>(null);

  useLayoutEffect(() => {
    const node = messagesRef.current;
    if (!node) return;
    const preserved = preserveScrollRef.current;
    if (preserved) {
      node.scrollTop = node.scrollHeight - preserved.height + preserved.top;
      preserveScrollRef.current = null;
      return;
    }
    node.scrollTo({ top: node.scrollHeight });
    // Re-scroll as the live ActivityCard grows: runEvents updates each step and
    // the in-flight card's body expands, so we need to keep the bottom in view.
  }, [session.messages, session.status, session.runEvents, session.approvals]);

  function loadOlder() {
    const node = messagesRef.current;
    if (node) {
      preserveScrollRef.current = { height: node.scrollHeight, top: node.scrollTop };
    }
    session.loadOlder();
  }

  return (
    <main className="chat-shell">
      <div className="messages" ref={messagesRef}>
        {session.messages.length > 0 && session.historyHasMore && (
          <div className="history-loader">
            <button disabled={session.loadingHistory} onClick={loadOlder}>
              {session.loadingHistory ? "Загружаю..." : "Показать более ранние"}
            </button>
          </div>
        )}
        {session.messages.length === 0 && (
          <div className="empty-chat">
            <Bot size={32} />
            <strong>{user.name}, рабочая сессия готова</strong>
            <span>Задачи и ответы появятся здесь.</span>
          </div>
        )}
        {session.messages.flatMap((message) => {
          const bubble = (
            <MessageBubble key={message.id} message={message} onPreviewFile={onPreviewFile} />
          );
          // After a user message that opened a request, render its ActivityCard.
          // The card is omitted when there's no timeline data and the request
          // isn't active (e.g. history reloaded without persisted events).
          if (message.role === "user" && message.request_id) {
            const rid = message.request_id;
            const events = session.runEvents.filter((event) => event.requestId === rid);
            const approvals = session.approvals.filter((approval) => approval.request_id === rid);
            const isActive = session.status.requestId === rid && session.status.active;
            if (events.length > 0 || approvals.length > 0 || isActive) {
              return [
                bubble,
                <ActivityCard
                  key={`activity_${rid}`}
                  requestId={rid}
                  events={events}
                  approvals={approvals}
                  isActive={isActive}
                  statusLabel={session.status.label}
                  statusTone={session.status.tone}
                  onAnswerApproval={session.answerApproval}
                />
              ];
            }
          }
          return [bubble];
        })}
      </div>
      <StatusLineView status={session.status} connected={session.connected} />
      <footer className="composer">
        <textarea
          value={session.input}
          placeholder="Введите сообщение или задачу"
          onChange={(event) => session.setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              session.send();
            }
          }}
        />
        <button
          className="send-button"
          disabled={!session.connected || !session.input.trim()}
          onClick={session.send}
        >
          <Send size={18} />
        </button>
      </footer>
      <ContextSizeBar usage={contextUsage} />
    </main>
  );
}

function MessageBubble({
  message,
  onPreviewFile
}: {
  message: ChatMessage;
  onPreviewFile: (path: string) => void;
}) {
  const roleLabel =
    message.role === "user" ? "Вы" : message.role === "assistant" ? "CorpClaw" : "Система";
  const filePath = message.file?.path || "";
  return (
    <article className={`message ${message.role} ${message.tone || "normal"}`}>
      <div className="message-role">{roleLabel}</div>
      {message.file ? (
        <div className="file-message">
          <div>
            <strong title={message.file.name}>{message.file.name}</strong>
            <span>{message.file.caption || message.text}</span>
          </div>
          <div className="file-message-actions">
            {filePath && (
              <button onClick={() => onPreviewFile(filePath)}>
                <Eye size={16} /> Просмотр
              </button>
            )}
            {message.file.url ? (
              <a
                className="primary link-button"
                href={message.file.url}
                download={message.file.name}
              >
                <Download size={16} /> Скачать
              </a>
            ) : (
              <span className="file-unavailable">Недоступен</span>
            )}
          </div>
        </div>
      ) : message.role === "assistant" ? (
        <MarkdownMessage text={message.text} />
      ) : (
        <div className="message-text">{message.text}</div>
      )}
    </article>
  );
}

function StatusLineView({ status, connected }: { status: StatusLine; connected: boolean }) {
  if (!status.active) {
    return (
      <div className={`status-line ${connected ? "idle" : "warning"}`}>
        {connected ? <CheckCircle2 size={15} /> : <CircleAlert size={15} />}
        <span>{connected ? "Готов" : "Нет соединения"}</span>
      </div>
    );
  }
  return (
    <div className={`status-line ${status.tone}`}>
      <span className="pulse" />
      <Sparkles size={15} />
      <span>{status.label}</span>
    </div>
  );
}
