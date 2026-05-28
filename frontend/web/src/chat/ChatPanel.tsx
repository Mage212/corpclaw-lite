import { Bot, CheckCircle2, CircleAlert, Download, Send, Sparkles } from "lucide-react";
import type { Dispatch, SetStateAction } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
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
  const wsRef = useRef<WebSocket | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const resetSignalRef = useRef(resetSignal);

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
      handleWsEvent(parsed, addMessage, setMessages, setStatus, setApprovals, onContextUsage);
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
      {message.file ? (
        <div className="file-message">
          <div>
            <strong title={message.file.name}>{message.file.name}</strong>
            <span>{message.file.caption || message.text}</span>
          </div>
          <a className="primary link-button" href={message.file.url} download={message.file.name}>
            <Download size={16} /> Скачать
          </a>
        </div>
      ) : message.role === "assistant" ? (
        <MarkdownMessage text={message.text} />
      ) : (
        <div className="message-text">{message.text}</div>
      )}
    </article>
  );
}

function handleWsEvent(
  event: Record<string, unknown>,
  addMessage: (message: ChatMessage) => void,
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>,
  setStatus: Dispatch<SetStateAction<StatusLine>>,
  setApprovals: Dispatch<SetStateAction<ApprovalRequest[]>>,
  onContextUsage: (usage: ContextUsage) => void
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
    setStatus({
      active: true,
      requestId: null,
      label: String(event.message || "Сессия сброшена"),
      phase: "done",
      tone: "done"
    });
    window.setTimeout(() => setStatus(emptyStatus), 1600);
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
    const usage = parseContextUsage(event.usage);
    if (usage) {
      onContextUsage(usage);
    }
    addMessage({
      id: id("error"),
      role: "system",
      text: String(event.message || "Ошибка"),
      tone: "error"
    });
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
