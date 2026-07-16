import {
  ArrowLeft,
  CalendarClock,
  Check,
  Pause,
  Pencil,
  Play,
  RefreshCw,
  Sparkles,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  acceptSchedule,
  dismissSchedule,
  listSchedule,
  parseAssistSchedule,
  pauseSchedule,
  resumeSchedule,
  type ScheduleParseAssist
} from "../api";
import { Modal } from "../components/Modal";
import { SCHEDULE_LABEL } from "../i18n/ru";
import type { ScheduleTask, ScheduleTaskStatus } from "../types";

export type ScheduleViewProps = {
  csrf: string;
  onBack: () => void;
  /** Notify parent of pending count (sidebar badge). */
  onPendingCountChange?: (count: number) => void;
};

type FilterTab = "live" | "history";

const STATUS_LABEL: Record<ScheduleTaskStatus, string> = {
  pending: "Ожидает",
  active: "Активна",
  paused: "Пауза",
  done: "Завершена",
  dismissed: "Отклонена"
};

const KIND_LABEL: Record<string, string> = {
  once: "разово",
  interval: "интервал",
  cron: "cron",
  unset: "не распознано"
};

/**
 * B-140: «Мои задачи» — list + consent confirm for scheduled agent runs.
 * Not a Chat/Work tab: separate management view (like Extensions).
 * Accept/Dismiss/Pause/Resume only (human); no agent self-accept.
 */
export function ScheduleView({ csrf, onBack, onPendingCountChange }: ScheduleViewProps) {
  const [tasks, setTasks] = useState<ScheduleTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [tab, setTab] = useState<FilterTab>("live");
  const [editing, setEditing] = useState<ScheduleTask | null>(null);
  const [assistFor, setAssistFor] = useState<ScheduleTask | null>(null);
  const [assist, setAssist] = useState<ScheduleParseAssist | null>(null);
  const [assistBusy, setAssistBusy] = useState(false);
  const [assistError, setAssistError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    setLoading(true);
    setError(null);
    listSchedule(csrf)
      .then((items) => {
        setTasks(items);
        const pending = items.filter((t) => t.status === "pending").length;
        onPendingCountChange?.(pending);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Не удалось загрузить задачи");
      })
      .finally(() => setLoading(false));
  }, [csrf, onPendingCountChange]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const liveTasks = useMemo(
    () => tasks.filter((t) => t.status === "pending" || t.status === "active" || t.status === "paused"),
    [tasks]
  );
  const historyTasks = useMemo(
    () => tasks.filter((t) => t.status === "done" || t.status === "dismissed"),
    [tasks]
  );

  const pending = useMemo(() => liveTasks.filter((t) => t.status === "pending"), [liveTasks]);
  const active = useMemo(() => liveTasks.filter((t) => t.status === "active"), [liveTasks]);
  const paused = useMemo(() => liveTasks.filter((t) => t.status === "paused"), [liveTasks]);

  async function runAction(
    taskId: string,
    action: () => Promise<ScheduleTask>
  ): Promise<void> {
    setBusyId(taskId);
    setError(null);
    try {
      await action();
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Действие не выполнено");
    } finally {
      setBusyId(null);
    }
  }

  function handleAccept(task: ScheduleTask) {
    void runAction(task.id, () => acceptSchedule(csrf, task.id));
  }

  function handleDismiss(task: ScheduleTask) {
    void runAction(task.id, () => dismissSchedule(csrf, task.id));
  }

  function handlePause(task: ScheduleTask) {
    void runAction(task.id, () => pauseSchedule(csrf, task.id));
  }

  function handleResume(task: ScheduleTask) {
    void runAction(task.id, () => resumeSchedule(csrf, task.id));
  }

  async function handleEditSubmit(overrides: {
    title: string;
    task_text: string;
    schedule_text: string;
  }) {
    if (!editing) return;
    const taskId = editing.id;
    setBusyId(taskId);
    setError(null);
    try {
      await acceptSchedule(csrf, taskId, overrides);
      setEditing(null);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось подтвердить");
    } finally {
      setBusyId(null);
    }
  }

  async function handleParseAssist(task: ScheduleTask, scheduleText?: string) {
    setAssistFor(task);
    setAssist(null);
    setAssistError(null);
    setAssistBusy(true);
    try {
      const result = await parseAssistSchedule(csrf, task.id, scheduleText);
      setAssist(result);
    } catch (err) {
      setAssistError(err instanceof Error ? err.message : "Не удалось разобрать расписание");
    } finally {
      setAssistBusy(false);
    }
  }

  async function handleAcceptWithFormula() {
    if (!assistFor || !assist) return;
    const taskId = assistFor.id;
    setBusyId(taskId);
    setError(null);
    try {
      await acceptSchedule(csrf, taskId, { schedule_text: assist.formula });
      setAssistFor(null);
      setAssist(null);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось подтвердить");
    } finally {
      setBusyId(null);
    }
  }

  const shown = tab === "live" ? liveTasks : historyTasks;

  return (
    <main className="schedule-view">
      <header className="extensions-header">
        <button className="icon-button extensions-back-btn" onClick={onBack} title="Назад к чату">
          <ArrowLeft size={18} />
        </button>
        <h2 className="extensions-title">
          <CalendarClock size={18} />
          <span>{SCHEDULE_LABEL}</span>
          {pending.length > 0 && (
            <span className="schedule-pending-pill" title="Ожидают подтверждения">
              {pending.length}
            </span>
          )}
        </h2>
        <button
          className="extensions-reload-btn"
          onClick={refresh}
          disabled={loading}
          title="Обновить список"
        >
          <RefreshCw size={15} className={loading ? "spin" : ""} />
          <span>{loading ? "Загрузка…" : "Обновить"}</span>
        </button>
      </header>

      <div className="extensions-tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "live"}
          className={`extensions-tab ${tab === "live" ? "active" : ""}`}
          onClick={() => setTab("live")}
        >
          Текущие
          {liveTasks.length > 0 ? ` (${liveTasks.length})` : ""}
        </button>
        <button
          role="tab"
          aria-selected={tab === "history"}
          className={`extensions-tab ${tab === "history" ? "active" : ""}`}
          onClick={() => setTab("history")}
        >
          История
          {historyTasks.length > 0 ? ` (${historyTasks.length})` : ""}
        </button>
      </div>

      <div className="schedule-body">
        {error && <div className="schedule-error">{error}</div>}

        {loading && tasks.length === 0 ? (
          <div className="extensions-placeholder">Загрузка…</div>
        ) : shown.length === 0 ? (
          <div className="extensions-placeholder">
            {tab === "live"
              ? "Нет активных или ожидающих задач. Попросите агента поставить задачу по расписанию — она появится здесь для подтверждения."
              : "История пуста."}
          </div>
        ) : tab === "live" ? (
          <div className="schedule-sections">
            {pending.length > 0 && (
              <ScheduleSection title="Ожидают подтверждения" tone="pending">
                {pending.map((task) => (
                  <ScheduleCard
                    key={task.id}
                    task={task}
                    busy={busyId === task.id || assistBusy}
                    onAccept={() => handleAccept(task)}
                    onEdit={() => setEditing(task)}
                    onDismiss={() => handleDismiss(task)}
                    onPause={() => handlePause(task)}
                    onResume={() => handleResume(task)}
                    onParseAssist={() => void handleParseAssist(task)}
                  />
                ))}
              </ScheduleSection>
            )}
            {active.length > 0 && (
              <ScheduleSection title="Активные" tone="active">
                {active.map((task) => (
                  <ScheduleCard
                    key={task.id}
                    task={task}
                    busy={busyId === task.id}
                    onAccept={() => handleAccept(task)}
                    onEdit={() => setEditing(task)}
                    onDismiss={() => handleDismiss(task)}
                    onPause={() => handlePause(task)}
                    onResume={() => handleResume(task)}
                  />
                ))}
              </ScheduleSection>
            )}
            {paused.length > 0 && (
              <ScheduleSection title="На паузе" tone="paused">
                {paused.map((task) => (
                  <ScheduleCard
                    key={task.id}
                    task={task}
                    busy={busyId === task.id}
                    onAccept={() => handleAccept(task)}
                    onEdit={() => setEditing(task)}
                    onDismiss={() => handleDismiss(task)}
                    onPause={() => handlePause(task)}
                    onResume={() => handleResume(task)}
                  />
                ))}
              </ScheduleSection>
            )}
          </div>
        ) : (
          <div className="schedule-list">
            {historyTasks.map((task) => (
              <ScheduleCard
                key={task.id}
                task={task}
                busy={false}
                readOnly
                onAccept={() => undefined}
                onEdit={() => undefined}
                onDismiss={() => undefined}
                onPause={() => undefined}
                onResume={() => undefined}
              />
            ))}
          </div>
        )}
      </div>

      {editing && (
        <EditAcceptModal
          task={editing}
          busy={busyId === editing.id || assistBusy}
          csrf={csrf}
          onClose={() => setEditing(null)}
          onSubmit={handleEditSubmit}
          onParseAssist={(scheduleText) => void handleParseAssist(editing, scheduleText)}
        />
      )}

      {assistFor && (
        <AssistModal
          task={assistFor}
          assist={assist}
          busy={assistBusy || busyId === assistFor.id}
          error={assistError}
          onClose={() => {
            setAssistFor(null);
            setAssist(null);
            setAssistError(null);
          }}
          onAcceptFormula={() => void handleAcceptWithFormula()}
          onRetry={() => void handleParseAssist(assistFor)}
        />
      )}
    </main>
  );
}

function ScheduleSection({
  title,
  tone,
  children
}: {
  title: string;
  tone: "pending" | "active" | "paused";
  children: ReactNode;
}) {
  return (
    <section className={`schedule-section schedule-section-${tone}`}>
      <h3 className="schedule-section-title">{title}</h3>
      <div className="schedule-list">{children}</div>
    </section>
  );
}

function ScheduleCard({
  task,
  busy,
  readOnly = false,
  onAccept,
  onEdit,
  onDismiss,
  onPause,
  onResume,
  onParseAssist
}: {
  task: ScheduleTask;
  busy: boolean;
  readOnly?: boolean;
  onAccept: () => void;
  onEdit: () => void;
  onDismiss: () => void;
  onPause: () => void;
  onResume: () => void;
  onParseAssist?: () => void;
}) {
  const statusClass =
    task.status === "pending"
      ? "pending"
      : task.status === "active"
        ? "ok"
        : task.status === "paused"
          ? "warning"
          : task.status === "dismissed"
            ? "error"
            : "muted";

  const kindLabel = KIND_LABEL[task.schedule.kind] ?? task.schedule.kind;
  const parseHint =
    task.schedule.kind === "unset"
      ? "формула не распознана — при подтверждении укажите понятное расписание (every 1h, tomorrow 9:00, cron …)"
      : formatScheduleDetail(task);

  return (
    <article className={`schedule-card status-${task.status}`}>
      <div className="schedule-card-header">
        <span className="schedule-card-title">{task.title}</span>
        <span className={`schedule-status-badge ${statusClass}`}>
          {STATUS_LABEL[task.status]}
        </span>
      </div>
      <p className="schedule-card-task">{task.task_text}</p>
      <div className="schedule-card-meta">
        <span>
          <strong>Когда:</strong> {task.schedule_text}
        </span>
        <span>
          <strong>Разбор:</strong> {kindLabel}
          {parseHint ? ` · ${parseHint}` : ""}
        </span>
        <span>
          <strong>TZ:</strong> {task.timezone}
        </span>
        {task.next_run_at && (
          <span>
            <strong>След. запуск:</strong> {formatWhen(task.next_run_at)}
          </span>
        )}
        {task.last_run_at && (
          <span>
            <strong>Последний:</strong> {formatWhen(task.last_run_at)}
            {task.last_status ? ` (${task.last_status})` : ""}
          </span>
        )}
        {task.run_count > 0 && (
          <span>
            <strong>Запусков:</strong> {task.run_count}
            {task.error_count > 0 ? ` · ошибок ${task.error_count}` : ""}
          </span>
        )}
      </div>

      {!readOnly && (
        <div className="schedule-card-actions">
          {task.status === "pending" && (
            <>
              <button
                type="button"
                className="schedule-btn primary"
                disabled={busy || task.schedule.kind === "unset"}
                onClick={onAccept}
                title={
                  task.schedule.kind === "unset"
                    ? "Сначала разберите или укажите формулу"
                    : "Подтвердить как есть"
                }
              >
                <Check size={14} />
                <span>Подтвердить</span>
              </button>
              {task.schedule.kind === "unset" && onParseAssist && (
                <button
                  type="button"
                  className="schedule-btn"
                  disabled={busy}
                  onClick={onParseAssist}
                  title="Разобрать фразу через LLM (один вызов)"
                >
                  <Sparkles size={14} />
                  <span>Разобрать</span>
                </button>
              )}
              <button
                type="button"
                className="schedule-btn"
                disabled={busy}
                onClick={onEdit}
                title="Изменить и подтвердить"
              >
                <Pencil size={14} />
                <span>Изменить</span>
              </button>
              <button
                type="button"
                className="schedule-btn danger"
                disabled={busy}
                onClick={onDismiss}
              >
                <X size={14} />
                <span>Отклонить</span>
              </button>
            </>
          )}
          {task.status === "active" && (
            <>
              <button type="button" className="schedule-btn" disabled={busy} onClick={onPause}>
                <Pause size={14} />
                <span>Пауза</span>
              </button>
              <button
                type="button"
                className="schedule-btn danger"
                disabled={busy}
                onClick={onDismiss}
              >
                <X size={14} />
                <span>Отменить</span>
              </button>
            </>
          )}
          {task.status === "paused" && (
            <>
              <button type="button" className="schedule-btn primary" disabled={busy} onClick={onResume}>
                <Play size={14} />
                <span>Возобновить</span>
              </button>
              <button
                type="button"
                className="schedule-btn danger"
                disabled={busy}
                onClick={onDismiss}
              >
                <X size={14} />
                <span>Отменить</span>
              </button>
            </>
          )}
        </div>
      )}
    </article>
  );
}

function EditAcceptModal({
  task,
  busy,
  csrf: _csrf,
  onClose,
  onSubmit,
  onParseAssist
}: {
  task: ScheduleTask;
  busy: boolean;
  csrf: string;
  onClose: () => void;
  onSubmit: (overrides: {
    title: string;
    task_text: string;
    schedule_text: string;
  }) => void | Promise<void>;
  onParseAssist?: (scheduleText: string) => void;
}) {
  const [title, setTitle] = useState(task.title);
  const [taskText, setTaskText] = useState(task.task_text);
  const [scheduleText, setScheduleText] = useState(task.schedule_text);

  return (
    <Modal
      title="Изменить и подтвердить"
      description="После подтверждения задача станет активной и будет запускаться по расписанию."
      onClose={onClose}
      onSubmit={() =>
        onSubmit({
          title: title.trim(),
          task_text: taskText.trim(),
          schedule_text: scheduleText.trim()
        })
      }
      footer={
        <>
          {onParseAssist && (
            <button
              type="button"
              className="schedule-btn"
              disabled={busy}
              onClick={() => onParseAssist(scheduleText.trim())}
            >
              <Sparkles size={14} />
              <span>Разобрать</span>
            </button>
          )}
          <button type="button" className="schedule-btn" onClick={onClose} disabled={busy}>
            Отмена
          </button>
          <button type="submit" className="schedule-btn primary" disabled={busy}>
            {busy ? "Сохраняю…" : "Подтвердить"}
          </button>
        </>
      }
    >
      <label className="schedule-field">
        Название
        <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} />
      </label>
      <label className="schedule-field">
        Задание для агента
        <textarea
          value={taskText}
          onChange={(e) => setTaskText(e.target.value)}
          rows={4}
        />
      </label>
      <label className="schedule-field">
        Расписание
        <input
          value={scheduleText}
          onChange={(e) => setScheduleText(e.target.value)}
          placeholder="every 1h · tomorrow 9:00 · 0 9 * * 1-5"
        />
      </label>
      <p className="schedule-field-hint">
        Примеры: <code>every 30m</code>, <code>every 1h</code>, <code>0 9 * * 1-5</code> (cron).
        Свободную фразу можно разобрать кнопкой «Разобрать» (один LLM-вызов). Часовой пояс:{" "}
        {task.timezone}.
      </p>
    </Modal>
  );
}

function AssistModal({
  task,
  assist,
  busy,
  error,
  onClose,
  onAcceptFormula,
  onRetry
}: {
  task: ScheduleTask;
  assist: ScheduleParseAssist | null;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onAcceptFormula: () => void;
  onRetry: () => void;
}) {
  return (
    <Modal
      title="Как понял расписание"
      description={`«${task.schedule_text}» — проверка перед активацией.`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="schedule-btn" onClick={onClose} disabled={busy}>
            Закрыть
          </button>
          {assist && (
            <button
              type="button"
              className="schedule-btn primary"
              disabled={busy}
              onClick={onAcceptFormula}
            >
              <Check size={14} />
              <span>{busy ? "Сохраняю…" : "Подтвердить так"}</span>
            </button>
          )}
          {!assist && (
            <button type="button" className="schedule-btn" disabled={busy} onClick={onRetry}>
              {busy ? "Думаю…" : "Повторить"}
            </button>
          )}
        </>
      }
    >
      {busy && !assist && <p className="schedule-field-hint">Разбираю фразу…</p>}
      {error && <div className="schedule-error">{error}</div>}
      {assist && (
        <div className="schedule-assist-result">
          <p>
            <strong>Формула:</strong> <code>{assist.formula}</code>
          </p>
          <p>
            <strong>Пояснение:</strong> {assist.explanation}
          </p>
          <p>
            <strong>Вид:</strong> {assist.schedule.kind}
            {assist.next_run_at ? ` · след. запуск UTC ${assist.next_run_at}` : ""}
          </p>
        </div>
      )}
    </Modal>
  );
}

function formatScheduleDetail(task: ScheduleTask): string {
  const s = task.schedule;
  if (s.kind === "once" && s.run_at) return formatWhen(s.run_at);
  if (s.kind === "interval" && s.minutes != null) {
    if (s.minutes % 60 === 0) return `каждые ${s.minutes / 60} ч`;
    return `каждые ${s.minutes} мин`;
  }
  if (s.kind === "cron" && s.expr) return s.expr;
  return "";
}

function formatWhen(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  } catch {
    return iso;
  }
}
