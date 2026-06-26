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
  DepthMode,
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

/** Per-request cap so a chatty run can't evict other requests' timelines. */
const MAX_EVENTS_PER_REQUEST = 60;

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
  return [...events, makeTimelineEvent(event)].slice(-MAX_EVENTS_PER_REQUEST);
}

/**
 * Timeline events scoped per-request. A flat capped array would evict older
 * requests' events once the global cap filled, making their ActivityCards
 * vanish. Scoping by requestId (capped per-request) keeps each card populated.
 * Events with `requestId === null` (global: file_ready, ws-down) are bucketed
 * under "" and shown in no ActivityCard (they render as system bubbles).
 */
type TimelineByRequest = Map<string, RunTimelineEvent[]>;

const GLOBAL_BUCKET = "";

function appendScoped(
  byRequest: TimelineByRequest,
  event: Omit<RunTimelineEvent, "id" | "createdAt">
): TimelineByRequest {
  const key = event.requestId ?? GLOBAL_BUCKET;
  const next = new Map(byRequest);
  next.set(key, appendTimeline(byRequest.get(key) ?? [], event));
  return next;
}

type UseWebChatSessionOptions = {
  csrf: string;
  mode: AgentMode;
  /** Processing depth (Etap 3): Fast = no thinking, Think = reasoning on. */
  depthMode: DepthMode;
  resetSignal: number;
  onContextUsage: (usage: ContextUsage) => void;
  onWorkspaceChanged?: (() => void) | undefined;
  /**
   * The chat to view. `null` = follow the active chat (load the active session
   * on connect). A specific id = load that chat's transcript read-only (the
   * client activates it separately via POST /api/chats/{id}/activate).
   */
  chatId?: number | null;
  /** Fired when the server signals a chat was renamed (to refresh the list). */
  onChatRenamed?: (() => void) | undefined;
  /** Fired when the server signals the chat list changed (create/activate/rename). */
  onChatListChanged?: (() => void) | undefined;
};

export type WebChatSession = {
  messages: ChatMessage[];
  status: StatusLine;
  approvals: ApprovalRequest[];
  input: string;
  connected: boolean;
  historyHasMore: boolean;
  loadingHistory: boolean;
  /** Timeline events grouped by request id. Use `.get(requestId)` for a card. */
  runEventsByRequest: TimelineByRequest;
  /** True when viewing a non-active chat (composer should be disabled). */
  readOnly: boolean;
  setInput: (value: string) => void;
  send: () => void;
  loadOlder: () => void;
  answerApproval: (approvalId: string, approved: boolean) => void;
  resetContext: () => void;
};

export function useWebChatSession({
  csrf,
  mode,
  depthMode,
  resetSignal,
  onContextUsage,
  onWorkspaceChanged,
  chatId = null,
  onChatRenamed,
  onChatListChanged
}: UseWebChatSessionOptions): WebChatSession {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<StatusLine>(emptyStatus);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [readOnly, setReadOnly] = useState(false);
  const [runEventsByRequest, setRunEventsByRequest] = useState<TimelineByRequest>(
    () => new Map()
  );
  const wsRef = useRef<WebSocket | null>(null);
  const resetSignalRef = useRef(resetSignal);
  const modeRef = useRef(mode);
  const depthModeRef = useRef(depthMode);
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
    setRunEventsByRequest((items) => appendScoped(items, event));
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
    depthModeRef.current = depthMode;
  }, [depthMode]);

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
          ws?.send(
            JSON.stringify({ type: "depth_mode_change", depth_mode: depthModeRef.current })
          );
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
            setRunEventsByRequest,
            onWorkspaceChanged,
            getActiveRequestId: () => lastActiveRequestIdRef.current,
            setActiveRequestId: (requestId) => {
              lastActiveRequestIdRef.current = requestId;
            },
            setReadOnly,
            onChatRenamed,
            onChatListChanged
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
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "depth_mode_change", depth_mode: depthMode }));
    }
  }, [depthMode]);

  useEffect(() => {
    if (resetSignal === resetSignalRef.current) return;
    resetSignalRef.current = resetSignal;
    resetContext();
  }, [resetContext, resetSignal]);

  // Etap 2: when the viewed chat changes, request its transcript read-only.
  // chatId === null means "follow the active chat" (loaded on connect); a
  // specific id triggers a WS load_chat. Activation (making it the active chat
  // the agent writes to) is a separate REST call done by the caller.
  useEffect(() => {
    if (chatId == null) return;
    const ws = wsRef.current;
    if (ws?.readyState !== WebSocket.OPEN) return;
    setLoadingHistory(true);
    ws.send(JSON.stringify({ type: "load_chat", session_id: chatId }));
  }, [chatId]);

  return {
    messages,
    status,
    approvals,
    input,
    connected,
    historyHasMore,
    loadingHistory,
    runEventsByRequest,
    readOnly,
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
  setRunEventsByRequest: Dispatch<SetStateAction<TimelineByRequest>>;
  onWorkspaceChanged: (() => void) | undefined;
  /** Read the last-known active request id (for stamping approvals). */
  getActiveRequestId: () => string | null;
  /** Record a request id as the active one (called on request_started/state/finished/status_update). */
  setActiveRequestId: (requestId: string) => void;
  /** Set the read-only flag (true when viewing a non-active chat transcript). */
  setReadOnly: Dispatch<SetStateAction<boolean>>;
  /** Fired when a chat was renamed or the chat list changed (refresh sidebar). */
  onChatRenamed: (() => void) | undefined;
  onChatListChanged: (() => void) | undefined;
};

function pushRunEvent(
  handlers: Pick<WsEventHandlers, "setRunEventsByRequest">,
  event: Omit<RunTimelineEvent, "id" | "createdAt">
) {
  handlers.setRunEventsByRequest((items) => appendScoped(items, event));
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
    onWorkspaceChanged,
    setReadOnly,
    onChatRenamed,
    onChatListChanged
  } = handlers;
  if (event.type === "chat_history") {
    setMessages(event.messages);
    setHistoryHasMore(event.has_more);
    setLoadingHistory(false);
    setReadOnly(event.read_only === true);
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
    handlers.setRunEventsByRequest(() => {
      // Reset clears all per-request timelines and leaves only the reset marker.
      const reset = new Map<string, RunTimelineEvent[]>();
      reset.set(GLOBAL_BUCKET, [
        makeTimelineEvent({
          requestId: null,
          type: "reset",
          label: event.message,
          tone: "done"
        })
      ]);
      return reset;
    });
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
  } else if (event.type === "chat_renamed") {
    // A chat got an auto-generated title (or was renamed). Refresh the sidebar list.
    onChatRenamed?.();
  } else if (event.type === "chat_activated") {
    // The active chat changed (via POST .../activate). We're now following it:
    // drop read-only and let the next chat_history load repopulate messages.
    setReadOnly(false);
    onChatListChanged?.();
  } else if (event.type === "chat_list_changed") {
    onChatListChanged?.();
  }
}
