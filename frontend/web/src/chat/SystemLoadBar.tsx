import { Gauge } from "lucide-react";
import type { SystemLoad } from "../types";

export type SystemLoadBarProps = {
  load: SystemLoad | null;
  /** WebSocket connection state — bar stays visible but muted when offline. */
  connected: boolean;
};

/**
 * Always-on ambient GPU / multi-user load indicator (DC-008 / D-088).
 *
 * Counts only: LLM slots in use, queue waiters, in-flight workflows.
 * No personal queue position and no PII — those stay on request-scoped status.
 */
export function SystemLoadBar({ load, connected }: SystemLoadBarProps) {
  if (load === null) {
    return (
      <div
        className={`system-load-bar ${connected ? "" : "offline"}`}
        title="Ожидание данных о загрузке системы…"
      >
        <span className="system-load-label">
          <Gauge size={13} />
          <span>Система</span>
        </span>
        <span className="system-load-value">
          {connected ? "загрузка…" : "нет связи"}
        </span>
      </div>
    );
  }

  const { active_count, max_concurrent, waiting_count, active_users, load_level } = load;
  const tone = load_level === "saturated" ? "danger" : load_level === "busy" ? "warning" : "";
  const valueLabel = `LLM ${active_count}/${max_concurrent} · в очереди ${waiting_count} · пользователей ${active_users}`;
  const title = [
    "Загрузка GPU/очереди (общая для всех).",
    "Слот освобождается после простоя и достаётся следующему в очереди.",
    connected ? "" : "Нет связи с сервером — данные могут быть устаревшими."
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={`system-load-bar ${tone} ${connected ? "" : "offline"}`.trim()}
      title={title}
    >
      <span className="system-load-label">
        <Gauge size={13} />
        <span>Система</span>
      </span>
      <span className="system-load-value">{valueLabel}</span>
      {!connected && <span className="system-load-offline">нет связи</span>}
    </div>
  );
}
