import { Bot, CheckCircle2, CircleAlert, Download, Send, Sparkles } from "lucide-react";
import type { Dispatch, SetStateAction } from "react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type {
  AgentMode,
  ApprovalRequest,
  ChatMessage,
  ContextUsage,
  StatusLine,
  User
} from "../types";
import { MarkdownMessage } from "./MarkdownMessage";

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

function parseContextUsage(value: unknown): ContextUsage | null {
  if (!isRecord(value)) return null;
  const latest = Number(value.latest_total_tokens || 0);
  const limit = Number(value.context_limit_tokens || 0);
  const ratio = Number(value.context_ratio || (limit > 0 ? latest / limit : 0));
  return {
    latest_total_tokens: latest,
    input_tokens: Number(value.input_tokens || 0),
    output_tokens: Number(value.output_tokens || 0),
    total_tokens: Number(value.total_tokens || 0),
    context_limit_tokens: limit,
    context_ratio: Math.max(0, Math.min(1, ratio))
  };
}

function parseChatMessage(value: unknown): ChatMessage | null {
  if (!isRecord(value)) return null;
  const role = value.role;
  if (role !== "user" && role !== "assistant" && role !== "system") return null;
  const dbId = typeof value.db_id === "number" ? value.db_id : undefined;
  const tone = value.tone;
  const fileValue = value.file;
  let file: ChatMessage["file"];
  if (isRecord(fileValue)) {
    const name = String(fileValue.name || "file");
    file = {
      name,
      url: typeof fileValue.url === "string" ? fileValue.url : undefined,
      caption: typeof fileValue.caption === "string" ? fileValue.caption : undefined,
      available: fileValue.available === true
    };
  }
  return {
    id: String(value.id || (dbId ? `db_${dbId}` : id("msg"))),
    db_id: dbId,
    session_id: typeof value.session_id === "number" ? value.session_id : undefined,
    role,
    text: String(value.text || ""),
    created_at: typeof value.created_at === "string" ? value.created_at : undefined,
    request_id: typeof value.request_id === "string" ? value.request_id : undefined,
    tone:
      tone === "warning" || tone === "error" || tone === "file" || tone === "normal"
        ? tone
        : undefined,
    file
  };
}

function parseChatMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  return value.map(parseChatMessage).filter((item): item is ChatMessage => item !== null);
}

function appendUnique(messages: ChatMessage[], message: ChatMessage): ChatMessage[] {
  if (message.db_id === undefined) {
    return [...messages, message];
  }
  if (messages.some((item) => item.db_id === message.db_id)) {
    return messages.map((item) => (item.db_id === message.db_id ? message : item));
  }
  return [...messages, message];
}

function prependUnique(messages: ChatMessage[], older: ChatMessage[]): ChatMessage[] {
  const known = new Set<number>();
  for (const message of messages) {
    if (message.db_id !== undefined) {
      known.add(message.db_id);
    }
  }
  const fresh = older.filter((item) => item.db_id === undefined || !known.has(item.db_id));
  return [...fresh, ...messages];
}

export function ChatPanel({
  csrf,
  mode,
  resetSignal,
  user,
  onContextUsage
}: {
  csrf: string;
  mode: AgentMode;
  resetSignal: number;
  user: User;
  onContextUsage: (usage: ContextUsage) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<StatusLine>(emptyStatus);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const resetSignalRef = useRef(resetSignal);
  const preserveScrollRef = useRef<{ height: number; top: number } | null>(null);

  const addMessage = useCallback((message: ChatMessage) => {
    setMessages((items) => appendUnique(items, message));
  }, []);

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(
      `${proto}://${window.location.host}/ws/chat?csrf=${encodeURIComponent(csrf)}`
    );
    wsRef.current = ws;
    ws.onopen = () => {
      setConnected(true);
      ws.send(JSON.stringify({ type: "mode_change", mode }));
    };
    ws.onclose = () => {
      setConnected(false);
      setStatus((current) =>
        current.active
          ? { ...current, tone: "warning", label: "Соединение с web-каналом закрыто" }
          : current
      );
    };
    ws.onmessage = (event) => {
      const parsed = JSON.parse(event.data) as unknown;
      if (!isRecord(parsed)) return;
      handleWsEvent(parsed, {
        addMessage,
        setMessages,
        setStatus,
        setApprovals,
        onContextUsage,
        setHistoryHasMore,
        setLoadingHistory
      });
    };
    return () => ws.close();
  }, [addMessage, csrf, onContextUsage]);

  useEffect(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "mode_change", mode }));
    }
  }, [mode]);

  useEffect(() => {
    if (resetSignal === resetSignalRef.current) return;
    resetSignalRef.current = resetSignal;
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "reset_context" }));
    } else {
      addMessage({
        id: id("reset_warning"),
        role: "system",
        text: "Нет соединения с web-каналом. Контекст не сброшен.",
        tone: "warning"
      });
    }
  }, [addMessage, resetSignal]);

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
  }, [messages, status]);

  function send() {
    const text = input.trim();
    if (!text || wsRef.current?.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({ type: "message", message: text }));
    setInput("");
  }

  function loadOlder() {
    if (!historyHasMore || loadingHistory || wsRef.current?.readyState !== WebSocket.OPEN) return;
    const firstPersisted = messages.find((message) => message.db_id !== undefined);
    if (!firstPersisted?.db_id) return;
    const node = messagesRef.current;
    if (node) {
      preserveScrollRef.current = { height: node.scrollHeight, top: node.scrollTop };
    }
    setLoadingHistory(true);
    wsRef.current.send(
      JSON.stringify({ type: "load_history_before", before_id: firstPersisted.db_id, limit: 100 })
    );
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
        {messages.length > 0 && historyHasMore && (
          <div className="history-loader">
            <button disabled={loadingHistory} onClick={loadOlder}>
              {loadingHistory ? "Загружаю..." : "Показать более ранние"}
            </button>
          </div>
        )}
        {messages.length === 0 && (
          <div className="empty-chat">
            <Bot size={32} />
            <strong>{user.name}, рабочая сессия готова</strong>
            <span>Задачи и ответы появятся здесь.</span>
          </div>
        )}
        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
        {approvals.map((approval) => (
          <div className="approval-card" key={approval.approval_id}>
            <div className="approval-title">
              <CircleAlert size={18} />
              <strong>{approval.action}</strong>
            </div>
            <p>{approval.details}</p>
            <div>
              <button
                className="primary"
                onClick={() => answerApproval(approval.approval_id, true)}
              >
                Разрешить
              </button>
              <button onClick={() => answerApproval(approval.approval_id, false)}>
                Отклонить
              </button>
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

function MessageBubble({ message }: { message: ChatMessage }) {
  const roleLabel =
    message.role === "user" ? "Вы" : message.role === "assistant" ? "CorpClaw" : "Система";
  return (
    <article className={`message ${message.role} ${message.tone || "normal"}`}>
      <div className="message-role">{roleLabel}</div>
      {message.file ? (
        <div className="file-message">
          <div>
            <strong title={message.file.name}>{message.file.name}</strong>
            <span>{message.file.caption || message.text}</span>
          </div>
          {message.file.url ? (
            <a className="primary link-button" href={message.file.url} download={message.file.name}>
              <Download size={16} /> Скачать
            </a>
          ) : (
            <span className="file-unavailable">Недоступен</span>
          )}
        </div>
      ) : message.role === "assistant" ? (
        <MarkdownMessage text={message.text} />
      ) : (
        <div className="message-text">{message.text}</div>
      )}
    </article>
  );
}

type WsEventHandlers = {
  addMessage: (message: ChatMessage) => void;
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  setStatus: Dispatch<SetStateAction<StatusLine>>;
  setApprovals: Dispatch<SetStateAction<ApprovalRequest[]>>;
  onContextUsage: (usage: ContextUsage) => void;
  setHistoryHasMore: Dispatch<SetStateAction<boolean>>;
  setLoadingHistory: Dispatch<SetStateAction<boolean>>;
};

function handleWsEvent(event: Record<string, unknown>, handlers: WsEventHandlers) {
  const {
    addMessage,
    setMessages,
    setStatus,
    setApprovals,
    onContextUsage,
    setHistoryHasMore,
    setLoadingHistory
  } = handlers;
  const type = event.type;
  if (type === "chat_history") {
    setMessages(parseChatMessages(event.messages));
    setHistoryHasMore(event.has_more === true);
    setLoadingHistory(false);
  } else if (type === "history_page") {
    const older = parseChatMessages(event.messages);
    setMessages((items) => prependUnique(items, older));
    setHistoryHasMore(event.has_more === true);
    setLoadingHistory(false);
  } else if (type === "chat_message") {
    const message = parseChatMessage(event.message);
    if (message) {
      addMessage(message);
    }
  } else if (type === "request_started" || type === "request_state") {
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
    const usage = parseContextUsage(event.usage);
    if (usage) {
      onContextUsage(usage);
    }
    setStatus({
      active: true,
      requestId: String(event.request_id || ""),
      label: String(event.label || "Готово"),
      phase: "done",
      tone
    });
    window.setTimeout(() => setStatus(emptyStatus), 1400);
  } else if (type === "context_usage") {
    const usage = parseContextUsage(event.usage);
    if (usage) {
      onContextUsage(usage);
    }
  } else if (type === "context_reset") {
    const usage = parseContextUsage(event.usage);
    if (usage) {
      onContextUsage(usage);
    }
    setMessages([]);
    setApprovals([]);
    setHistoryHasMore(false);
    setLoadingHistory(false);
    setStatus({
      active: true,
      requestId: null,
      label: String(event.message || "Сессия сброшена"),
      phase: "done",
      tone: "done"
    });
    window.setTimeout(() => setStatus(emptyStatus), 1600);
  } else if (type === "warning") {
    if (!event.request_id) {
      addMessage({
        id: id("warning"),
        role: "system",
        text: String(event.message || "Предупреждение"),
        tone: "warning"
      });
    }
    setStatus({
      active: true,
      requestId: String(event.request_id || ""),
      label: "Требуется внимание",
      phase: "warning",
      tone: "warning"
    });
  } else if (type === "error") {
    setLoadingHistory(false);
    const usage = parseContextUsage(event.usage);
    if (usage) {
      onContextUsage(usage);
    }
    if (!event.request_id) {
      addMessage({
        id: id("error"),
        role: "system",
        text: String(event.message || "Ошибка"),
        tone: "error"
      });
    }
  } else if (type === "file_ready") {
    const url = String(event.url || "");
    const name = String(event.name || "file");
    const caption = String(event.caption || "");
    addMessage({
      id: id("file"),
      role: "system",
      text: "Файл готов к скачиванию.",
      tone: "file",
      file: {
        name,
        url,
        caption
      }
    });
  } else if (type === "approval_required") {
    const approval = {
      approval_id: String(event.approval_id || ""),
      action: String(event.action || "Подтверждение"),
      details: String(event.details || "")
    };
    setApprovals((items) =>
      items.some((item) => item.approval_id === approval.approval_id)
        ? items.map((item) => (item.approval_id === approval.approval_id ? approval : item))
        : [...items, approval]
    );
  } else if (type === "approval_resolved") {
    const approvalId = String(event.approval_id || "");
    setApprovals((items) => items.filter((item) => item.approval_id !== approvalId));
  }
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
