import { Activity } from "lucide-react";
import type { ContextUsage } from "../types";

export type ContextSizeBarProps = {
  usage: ContextUsage | null;
};

/**
 * Compact context-size indicator rendered under the composer. Shows how full
 * the active conversation's context window is, so the user can judge whether a
 * follow-up request will fit or a compact is warranted.
 *
 * Etap 1B: the "сжать" (compact) button is decorative/disabled — the compact
 * action is future work. The bar reads the live `contextUsage` (last known
 * per-request usage); per-chat persisted sizes arrive with Etap 2.
 */
export function ContextSizeBar({ usage }: ContextSizeBarProps) {
  const latest = usage?.latest_total_tokens ?? 0;
  const limit = usage?.context_limit_tokens ?? 0;
  const ratio = usage?.context_ratio ?? 0;
  const percent = Math.round(ratio * 100);
  const tone = ratio >= 0.8 ? "danger" : ratio >= 0.6 ? "warning" : "";
  const valueLabel = limit ? `${formatTokens(latest)} / ${formatTokens(limit)} (${percent}%)` : "—";

  return (
    <div className={`context-size-bar ${tone}`} title={`Контекст: ${percent}%`}>
      <span className="context-size-label">
        <Activity size={13} />
        <span>Контекст чата</span>
      </span>
      <span className="context-size-track">
        <b style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
      </span>
      <span className="context-size-value">{valueLabel}</span>
      <button className="context-size-compact" disabled title="Сжатие контекста будет доступно позже">
        сжать
      </button>
    </div>
  );
}

function formatTokens(value: number): string {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`;
  }
  return String(value);
}
