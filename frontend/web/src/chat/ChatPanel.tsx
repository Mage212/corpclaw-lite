import { Bot, CheckCircle2, CircleAlert, Send, Sparkles } from "lucide-react";
import type { Dispatch, SetStateAction } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AgentMode,
  ApprovalRequest,
  ChatMessage,
  StatusLine,
  User
} from "../types";

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

export function ChatPanel({ csrf, mode, user }: { csrf: string; mode: AgentMode; user: User }) {
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
      <div className="message-text">{message.text}</div>
    </article>
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
