import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Brain,
  Check,
  ChevronDown,
  CircleAlert,
  Clock,
  FileText,
  Hourglass,
  ListChecks,
  Wrench,
  type LucideIcon
} from "lucide-react";
import type { ApprovalRequest, RunTimelineEvent, StatusLine } from "../types";

export type ActivityCardProps = {
  requestId: string;
  events: RunTimelineEvent[];
  approvals: ApprovalRequest[];
  isActive: boolean;
  statusLabel: string;
  statusTone: StatusLine["tone"];
  onAnswerApproval: (approvalId: string, approved: boolean) => void;
};

const STEP_ICON: Record<RunTimelineEvent["type"], LucideIcon> = {
  request: Hourglass,
  queue: Hourglass,
  llm: Brain,
  tool: Wrench,
  subagent: Bot,
  approval: CircleAlert,
  file: FileText,
  warning: AlertTriangle,
  error: AlertTriangle,
  done: Check,
  reset: ListChecks
};

/**
 * Inline run-timeline card, rendered between a user message and its assistant
 * response. Collapsible: auto-expanded while the request is running or awaiting
 * approval, auto-collapsed once done (but re-expandable by click).
 *
 * The caller pre-filters `events`/`approvals` by `requestId`; this component
 * only renders the filtered slice.
 */
export function ActivityCard({
  requestId,
  events,
  approvals,
  isActive,
  statusLabel,
  statusTone,
  onAnswerApproval
}: ActivityCardProps) {
  const awaitingApproval = approvals.length > 0;
  const shouldAutoExpand = isActive || awaitingApproval;
  const [expanded, setExpanded] = useState(shouldAutoExpand);

  // Auto-expand while running / awaiting approval; auto-collapse when it finishes.
  // We only react to transitions — a manual collapse during an active run is
  // respected until the run state actually changes.
  useEffect(() => {
    setExpanded(shouldAutoExpand);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shouldAutoExpand]);

  const tone = awaitingApproval
    ? "warning"
    : statusTone === "error"
      ? "error"
      : statusTone === "warning"
        ? "warning"
        : isActive
          ? "running"
          : "done";

  const headerLabel = isActive
    ? statusLabel || "Выполняется…"
    : lastNonMetaEvent(events)?.label || "Выполнено";
  const stepCount = events.filter((event) => !isMetaEvent(event)).length;
  const headerMeta = isActive
    ? "выполняется…"
    : `${stepCount} ${pluralizeSteps(stepCount)}${formatDuration(events)}`;

  return (
    <div
      className={`activity-card ${tone} ${expanded ? "expanded" : ""}`}
      data-request-id={requestId}
    >
      <div
        className="activity-header"
        onClick={() => setExpanded((value) => !value)}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setExpanded((value) => !value);
          }
        }}
      >
        <span className="activity-state-icon">
          {isActive ? <Clock size={15} /> : <ListChecks size={15} />}
        </span>
        <span className="activity-label" title={headerLabel}>
          {headerLabel}
        </span>
        <span className="activity-meta">{headerMeta}</span>
        <ChevronDown className="activity-chevron" size={15} />
      </div>

      {expanded && (
        <div className="activity-body">
          {events.length === 0 && isActive ? (
            <div className="activity-placeholder">Думаю…</div>
          ) : (
            events.map((event) => <ActivityStep key={event.id} event={event} />)
          )}

          {approvals.map((approval) => (
            <div className="activity-approval" key={approval.approval_id}>
              <div className="activity-approval-title">
                <CircleAlert size={15} />
                <span>{approval.action}</span>
              </div>
              <p>{approval.details}</p>
              <div className="activity-approval-actions">
                <button className="primary" onClick={() => onAnswerApproval(approval.approval_id, true)}>
                  Разрешить
                </button>
                <button onClick={() => onAnswerApproval(approval.approval_id, false)}>
                  Отклонить
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ActivityStep({ event }: { event: RunTimelineEvent }) {
  const Icon = STEP_ICON[event.type] ?? Hourglass;
  return (
    <div className={`activity-step ${event.tone} ${event.type}`}>
      <Icon size={14} />
      <span className="activity-step-label">
        {event.label}
        {event.detail && (
          <span className="activity-step-detail" title={event.detail}>
            {event.detail}
          </span>
        )}
      </span>
      <span className="activity-step-time">{event.createdAt}</span>
    </div>
  );
}

/** Events that are bookkeeping (start/done/reset) rather than concrete steps. */
function isMetaEvent(event: RunTimelineEvent): boolean {
  return event.type === "done" || event.type === "reset";
}

function lastNonMetaEvent(events: RunTimelineEvent[]): RunTimelineEvent | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event && !isMetaEvent(event)) {
      return event;
    }
  }
  return events[events.length - 1];
}

function pluralizeSteps(count: number): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return "шаг";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return "шага";
  return "шагов";
}

function formatDuration(events: RunTimelineEvent[]): string {
  if (events.length < 2) return "";
  // createdAt is "HH:MM"; coarse — only meaningful when both fall in the same hour.
  const first = events[0]?.createdAt;
  const last = events[events.length - 1]?.createdAt;
  if (!first || !last || first === last) return "";
  return ` · ${first}–${last}`;
}
