import { CalendarClock, Check, ExternalLink, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { acceptSchedule, dismissSchedule, getScheduleTask } from "../api";
import type { ScheduleConfirmMeta } from "../types";

export type ScheduleConfirmCardProps = {
  csrf: string;
  meta: ScheduleConfirmMeta;
  onResolved?: () => void;
  onOpenSchedule?: () => void;
};

type Resolved = "active" | "dismissed" | "done" | "other" | null;

/**
 * B-143: consent card in system inbox for schedule_propose.
 * Actions call B-141 REST (not agent chat). Edit lives in «Задачи».
 */
export function ScheduleConfirmCard({
  csrf,
  meta,
  onResolved,
  onOpenSchedule
}: ScheduleConfirmCardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resolved, setResolved] = useState<Resolved>(
    meta.status === "pending" || !meta.status ? null : mapStatus(meta.status)
  );

  const refreshStatus = useCallback(() => {
    getScheduleTask(csrf, meta.task_id)
      .then((task) => {
        if (task.status === "pending") {
          setResolved(null);
        } else {
          setResolved(mapStatus(task.status));
        }
      })
      .catch(() => {
        // Keep initial resolved from metadata if fetch fails (e.g. already gone).
      });
  }, [csrf, meta.task_id]);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  async function handleAccept() {
    setBusy(true);
    setError(null);
    try {
      await acceptSchedule(csrf, meta.task_id);
      setResolved("active");
      onResolved?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось подтвердить");
    } finally {
      setBusy(false);
    }
  }

  async function handleDismiss() {
    setBusy(true);
    setError(null);
    try {
      await dismissSchedule(csrf, meta.task_id);
      setResolved("dismissed");
      onResolved?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось отклонить");
    } finally {
      setBusy(false);
    }
  }

  const title = meta.title?.trim() || "Задача по расписанию";
  const when = meta.schedule_text?.trim() || "—";
  const kind = meta.schedule_kind || "unset";
  const taskPreview = meta.task_text?.trim();

  return (
    <div className={`schedule-confirm-card ${resolved ? "resolved" : "pending"}`}>
      <div className="schedule-confirm-header">
        <CalendarClock size={16} />
        <strong className="schedule-confirm-title">{title}</strong>
        <span className={`schedule-confirm-kind kind-${kind}`}>{kindLabel(kind)}</span>
      </div>
      <div className="schedule-confirm-meta">
        <span>
          <em>Когда:</em> {when}
        </span>
        {meta.timezone && (
          <span>
            <em>TZ:</em> {meta.timezone}
          </span>
        )}
      </div>
      {taskPreview && <p className="schedule-confirm-task">{taskPreview}</p>}

      {error && <div className="schedule-confirm-error">{error}</div>}

      {resolved ? (
        <div className="schedule-confirm-resolved">
          {resolvedLabel(resolved)}
          {onOpenSchedule && (
            <button type="button" className="schedule-confirm-link" onClick={onOpenSchedule}>
              <ExternalLink size={14} />
              <span>В Задачи</span>
            </button>
          )}
        </div>
      ) : (
        <div className="schedule-confirm-actions">
          <button
            type="button"
            className="schedule-btn primary"
            disabled={busy}
            onClick={() => void handleAccept()}
          >
            <Check size={14} />
            <span>Подтвердить</span>
          </button>
          <button
            type="button"
            className="schedule-btn danger"
            disabled={busy}
            onClick={() => void handleDismiss()}
          >
            <X size={14} />
            <span>Отклонить</span>
          </button>
          {onOpenSchedule && (
            <button type="button" className="schedule-btn" disabled={busy} onClick={onOpenSchedule}>
              <ExternalLink size={14} />
              <span>В Задачи</span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function parseScheduleConfirmMeta(
  metadata: Record<string, unknown> | null | undefined
): ScheduleConfirmMeta | null {
  if (!metadata || metadata.kind !== "schedule_confirm") return null;
  const taskId = metadata.task_id;
  if (typeof taskId !== "string" || !taskId.trim()) return null;
  const meta: ScheduleConfirmMeta = {
    kind: "schedule_confirm",
    task_id: taskId.trim()
  };
  if (typeof metadata.title === "string") meta.title = metadata.title;
  if (typeof metadata.task_text === "string") meta.task_text = metadata.task_text;
  if (typeof metadata.schedule_text === "string") meta.schedule_text = metadata.schedule_text;
  if (typeof metadata.schedule_kind === "string") meta.schedule_kind = metadata.schedule_kind;
  if (typeof metadata.timezone === "string") meta.timezone = metadata.timezone;
  if (typeof metadata.status === "string") meta.status = metadata.status;
  return meta;
}

function mapStatus(status: string): Resolved {
  if (status === "active") return "active";
  if (status === "dismissed") return "dismissed";
  if (status === "done") return "done";
  if (status === "paused") return "other";
  return "other";
}

function kindLabel(kind: string): string {
  switch (kind) {
    case "once":
      return "разово";
    case "interval":
      return "интервал";
    case "cron":
      return "cron";
    case "unset":
      return "не распознано";
    default:
      return kind;
  }
}

function resolvedLabel(resolved: Exclude<Resolved, null>): string {
  switch (resolved) {
    case "active":
      return "Подтверждено — задача активна.";
    case "dismissed":
      return "Отклонено.";
    case "done":
      return "Задача завершена.";
    default:
      return "Обработано (см. «Задачи»).";
  }
}
