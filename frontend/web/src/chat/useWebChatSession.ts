import type { Dispatch, SetStateAction } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createWebSocketTicket } from "../api";
import { parseServerWsEvent } from "../contracts";
import type { ServerWsEvent } from "../contracts";
import type {
  AgentMode,
  ApprovalRequest,
  ChatMessage,
  ContextUsage,
  RunTimelineEvent,
  StatusLine
} from "../types";

const emptyStatus: StatusLine = {
  active: false,
  requestId: null,
  label: "",
  phase: "idle",
  tone: "idle"
};

const MAX_TIMELINE_EVENTS = 80;

function id(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2)}_${Date.now()}`;
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

function timelineType(phase: string): RunTimelineEvent["type"] {
  if (phase === "queue") return "queue";
  if (phase === "llm") return "llm";
  if (phase === "tool") return "tool";
  if (phase === "subagent") return "subagent";
  return "request";
}

function nowLabel(): string {
  return new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function makeTimelineEvent(
  event: Omit<RunTimelineEvent, "id" | "createdAt">
): RunTimelineEvent {
  return {
    id: id("run"),
    createdAt: nowLabel(),
    ...event
  };
}

function appendTimeline(
  events: RunTimelineEvent[],
  event: Omit<RunTimelineEvent, "id" | "createdAt">
): RunTimelineEvent[] {
  const last = events[events.length - 1];
  if (
    last &&
    last.requestId === event.requestId &&
    last.type === event.type &&
    last.label === event.label &&
    last.detail === event.detail &&
    last.tone === event.tone
  ) {
    return events;
  }
  return [...events, makeTimelineEvent(event)].slice(-MAX_TIMELINE_EVENTS);
}

type UseWebChatSessionOptions = {
  csrf: string;
  mode: AgentMode;
  resetSignal: number;
  onContextUsage: (usage: ContextUsage) => void;
  onWorkspaceChanged?: (() => void) | undefined;
};

export type WebChatSession = {
  messages: ChatMessage[];
  status: StatusLine;
  approvals: ApprovalRequest[];
  input: string;
  connected: boolean;
  historyHasMore: boolean;
  loadingHistory: boolean;
  runEvents: RunTimelineEvent[];
  setInput: (value: string) => void;
  send: () => void;
  loadOlder: () => void;
  answerApproval: (approvalId: string, approved: boolean) => void;
  resetContext: () => void;
};

export function useWebChatSession({
  csrf,
  mode,
  resetSignal,
  onContextUsage,
  onWorkspaceChanged
}: UseWebChatSessionOptions): WebChatSession {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<StatusLine>(emptyStatus);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [runEvents, setRunEvents] = useState<RunTimelineEvent[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const resetSignalRef = useRef(resetSignal);
  const modeRef = useRef(mode);
  /**
   * Tracks the most recently seen request_id (from request_started/state/finished/
   * status_update). Used to stamp `request_id` onto approvals that arrive without
   * one on the wire — the orchestrator's approval_cb has no request in scope, but
   * approvals always fire mid-run while this ref holds the active id. Deliberately
   * NOT cleared on request_finished so approvals arriving in the (up to 300s)
   * approval window still get stamped, even after status auto-clears at 1.4s.
   */
  const lastActiveRequestIdRef = useRef<string | null>(null);

  const addMessage = useCallback((message: ChatMessage) => {
    setMessages((items) => appendUnique(items, message));
  }, []);

  const addRunEvent = useCallback((event: Omit<RunTimelineEvent, "id" | "createdAt">) => {
    setRunEvents((items) => appendTimeline(items, event));
  }, []);

  const resetContext = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "reset_context" }));
      return;
    }
    addMessage({
      id: id("reset_warning"),
      role: "system",
      text: "Нет соединения с web-каналом. Контекст не сброшен.",
      tone: "warning"
    });
    addRunEvent({
      requestId: null,
      type: "warning",
      label: "Контекст не сброшен",
      detail: "Нет соединения с web-каналом.",
      tone: "warning"
    });
  }, [addMessage, addRunEvent]);

  const send = useCallback(() => {
    const text = input.trim();
    if (!text || wsRef.current?.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({ type: "message", message: text }));
    setInput("");
  }, [input]);

  const loadOlder = useCallback(() => {
    if (!historyHasMore || loadingHistory || wsRef.current?.readyState !== WebSocket.OPEN) return;
    const firstPersisted = messages.find((message) => message.db_id !== undefined);
    if (!firstPersisted?.db_id) return;
    setLoadingHistory(true);
    wsRef.current.send(
      JSON.stringify({ type: "load_history_before", before_id: firstPersisted.db_id, limit: 100 })
    );
  }, [historyHasMore, loadingHistory, messages]);

  const answerApproval = useCallback((approvalId: string, approved: boolean) => {
    wsRef.current?.send(
      JSON.stringify({ type: approved ? "approve" : "deny", approval_id: approvalId })
    );
    setApprovals((items) => items.filter((item) => item.approval_id !== approvalId));
  }, []);

  useEffect(() => {
    modeRef.current = mode;
  }, [mode]);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let cancelled = false;

    createWebSocketTicket(csrf)
      .then(({ ticket }) => {
        if (cancelled) return;
        const proto = window.location.protocol === "https:" ? "wss" : "ws";
        ws = new WebSocket(
          `${proto}://${window.location.host}/ws/chat?ticket=${encodeURIComponent(ticket)}`
        );
        wsRef.current = ws;
        ws.onopen = () => {
          setConnected(true);
          ws?.send(JSON.stringify({ type: "mode_change", mode: modeRef.current }));
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
          if (typeof event.data !== "string") {
            console.warn("Ignored non-text WebSocket event");
            return;
          }
          let parsed: unknown;
          try {
            parsed = JSON.parse(event.data);
          } catch (error) {
            console.warn("Ignored invalid WebSocket JSON", error);
            return;
          }
          const wsEvent = parseServerWsEvent(parsed);
          if (wsEvent === null) {
            console.warn("Ignored unknown WebSocket event", parsed);
            return;
          }
          handleWsEvent(wsEvent, {
            addMessage,
            setMessages,
            setStatus,
            setApprovals,
            onContextUsage,
            setHistoryHasMore,
            setLoadingHistory,
            setRunEvents,
            onWorkspaceChanged,
            getActiveRequestId: () => lastActiveRequestIdRef.current,
            setActiveRequestId: (requestId) => {
              lastActiveRequestIdRef.current = requestId;
            }
          });
        };
      })
      .catch((error) => {
        if (cancelled) return;
        console.warn("Failed to create WebSocket ticket", error);
        setConnected(false);
        setStatus({
          active: false,
          requestId: null,
          phase: "ws",
          tone: "warning",
          label: "Не удалось открыть web-канал"
        });
        addRunEvent({
          requestId: null,
          type: "warning",
          label: "WebSocket недоступен",
          detail: "Интерфейс не смог открыть live-канал.",
          tone: "warning"
        });
      });

    return () => {
      cancelled = true;
      ws?.close();
      if (wsRef.current === ws) {
        wsRef.current = null;
      }
    };
  }, [addMessage, addRunEvent, csrf, onContextUsage, onWorkspaceChanged]);

  useEffect(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "mode_change", mode }));
    }
  }, [mode]);

  useEffect(() => {
    if (resetSignal === resetSignalRef.current) return;
    resetSignalRef.current = resetSignal;
    resetContext();
  }, [resetContext, resetSignal]);

  return {
    messages,
    status,
    approvals,
    input,
    connected,
    historyHasMore,
    loadingHistory,
    runEvents,
    setInput,
    send,
    loadOlder,
    answerApproval,
    resetContext
  };
}

type WsEventHandlers = {
  addMessage: (message: ChatMessage) => void;
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  setStatus: Dispatch<SetStateAction<StatusLine>>;
  setApprovals: Dispatch<SetStateAction<ApprovalRequest[]>>;
  onContextUsage: (usage: ContextUsage) => void;
  setHistoryHasMore: Dispatch<SetStateAction<boolean>>;
  setLoadingHistory: Dispatch<SetStateAction<boolean>>;
  setRunEvents: Dispatch<SetStateAction<RunTimelineEvent[]>>;
  onWorkspaceChanged: (() => void) | undefined;
  /** Read the last-known active request id (for stamping approvals). */
  getActiveRequestId: () => string | null;
  /** Record a request id as the active one (called on request_started/state/finished/status_update). */
  setActiveRequestId: (requestId: string) => void;
};

function pushRunEvent(
  handlers: Pick<WsEventHandlers, "setRunEvents">,
  event: Omit<RunTimelineEvent, "id" | "createdAt">
) {
  handlers.setRunEvents((items) => appendTimeline(items, event));
}

function handleWsEvent(event: ServerWsEvent, handlers: WsEventHandlers) {
  const {
    addMessage,
    setMessages,
    setStatus,
    setApprovals,
    onContextUsage,
    setHistoryHasMore,
    setLoadingHistory,
    onWorkspaceChanged
  } = handlers;
  if (event.type === "chat_history") {
    setMessages(event.messages);
    setHistoryHasMore(event.has_more);
    setLoadingHistory(false);
  } else if (event.type === "history_page") {
    setMessages((items) => prependUnique(items, event.messages));
    setHistoryHasMore(event.has_more);
    setLoadingHistory(false);
  } else if (event.type === "chat_message") {
    addMessage(event.message);
    if (event.message.file) {
      pushRunEvent(handlers, {
        requestId: event.message.request_id ?? null,
        type: "file",
        label: `Файл готов: ${event.message.file.name}`,
        detail: event.message.file.caption || event.message.file.path || undefined,
        tone: event.message.file.available === false ? "warning" : "done"
      });
      onWorkspaceChanged?.();
    }
  } else if (event.type === "request_started" || event.type === "request_state") {
    const phase = event.phase || "request";
    handlers.setActiveRequestId(event.request_id);
    setStatus({
      active: true,
      requestId: event.request_id,
      label: event.label,
      phase,
      tone: "running"
    });
    // Accumulate (don't reset): a fresh request_started now just appends its first
    // event so prior requests' timelines remain populated for their ActivityCards.
    pushRunEvent(handlers, {
      requestId: event.request_id,
      type: timelineType(phase),
      label: event.label,
      tone: "running"
    });
  } else if (event.type === "status_update") {
    handlers.setActiveRequestId(event.request_id);
    setStatus({
      active: true,
      requestId: event.request_id,
      label: event.label,
      phase: event.phase,
      tone: "running"
    });
    pushRunEvent(handlers, {
      requestId: event.request_id,
      type: timelineType(event.phase),
      label: event.label,
      tone: "running"
    });
  } else if (event.type === "status") {
    setStatus({
      active: true,
      requestId: null,
      label: `Статус: ${event.stage}`,
      phase: "legacy",
      tone: "running"
    });
  } else if (event.type === "assistant_message") {
    addMessage({
      id: id("assistant"),
      role: "assistant",
      text: event.message
    });
  } else if (event.type === "request_finished") {
    const tone = event.status === "error" ? "error" : event.status === "warning" ? "warning" : "done";
    handlers.setActiveRequestId(event.request_id);
    if (event.usage) {
      onContextUsage(event.usage);
    }
    setStatus({
      active: true,
      requestId: event.request_id,
      label: event.label,
      phase: "done",
      tone
    });
    pushRunEvent(handlers, {
      requestId: event.request_id,
      type: "done",
      label: event.label,
      tone
    });
    onWorkspaceChanged?.();
    window.setTimeout(() => setStatus(emptyStatus), 1400);
  } else if (event.type === "context_usage") {
    onContextUsage(event.usage);
  } else if (event.type === "context_reset") {
    if (event.usage) {
      onContextUsage(event.usage);
    }
    setMessages([]);
    setApprovals([]);
    setHistoryHasMore(false);
    setLoadingHistory(false);
    setStatus({
      active: true,
      requestId: null,
      label: event.message,
      phase: "done",
      tone: "done"
    });
    handlers.setRunEvents([
      makeTimelineEvent({
        requestId: null,
        type: "reset",
        label: event.message,
        tone: "done"
      })
    ]);
    onWorkspaceChanged?.();
    window.setTimeout(() => setStatus(emptyStatus), 1600);
  } else if (event.type === "warning") {
    if (!event.request_id) {
      addMessage({
        id: id("warning"),
        role: "system",
        text: event.message,
        tone: "warning"
      });
    }
    setStatus({
      active: true,
      requestId: event.request_id ?? "",
      label: "Требуется внимание",
      phase: "warning",
      tone: "warning"
    });
    pushRunEvent(handlers, {
      requestId: event.request_id ?? null,
      type: "warning",
      label: "Требуется внимание",
      detail: event.message,
      tone: "warning"
    });
  } else if (event.type === "error") {
    setLoadingHistory(false);
    if (event.usage) {
      onContextUsage(event.usage);
    }
    if (!event.request_id) {
      addMessage({
        id: id("error"),
        role: "system",
        text: event.message,
        tone: "error"
      });
    }
    pushRunEvent(handlers, {
      requestId: event.request_id ?? null,
      type: "error",
      label: "Ошибка выполнения",
      detail: event.message,
      tone: "error"
    });
  } else if (event.type === "file_ready") {
    addMessage({
      id: id("file"),
      role: "system",
      text: "Файл готов к скачиванию.",
      tone: "file",
      file: {
        name: event.name,
        path: event.path ?? null,
        url: event.url,
        caption: event.caption
      }
    });
    pushRunEvent(handlers, {
      requestId: null,
      type: "file",
      label: `Файл готов: ${event.name}`,
      detail: event.caption || event.path || undefined,
      tone: "done"
    });
    onWorkspaceChanged?.();
  } else if (event.type === "approval_required") {
    // The wire payload omits request_id (orchestrator approval_cb has no request
    // in scope). Prefer a backend-provided one if present; otherwise stamp the
    // last-known active request so the approval groups inside its ActivityCard.
    const requestId = event.request_id ?? handlers.getActiveRequestId();
    const approval: ApprovalRequest = { ...event, request_id: requestId };
    setApprovals((items) =>
      items.some((item) => item.approval_id === approval.approval_id)
        ? items.map((item) => (item.approval_id === approval.approval_id ? approval : item))
        : [...items, approval]
    );
    pushRunEvent(handlers, {
      requestId,
      type: "approval",
      label: approval.action,
      detail: approval.details,
      tone: "warning"
    });
  } else if (event.type === "approval_resolved") {
    setApprovals((items) => items.filter((item) => item.approval_id !== event.approval_id));
    pushRunEvent(handlers, {
      requestId: event.request_id ?? handlers.getActiveRequestId(),
      type: "done",
      label: "Подтверждение обработано",
      tone: "done"
    });
  }
}
