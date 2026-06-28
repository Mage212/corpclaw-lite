import { Activity } from "lucide-react";
import type { ContextUsage } from "../types";

export type ContextSizeBarProps = {
  usage: ContextUsage | null;
  /** Triggered by the "сжать" button (the hook handles the confirm dialog). */
  onCompress?: (() => void) | undefined;
  /** True while a compression request is in-flight (button shows "сжимаю…"). */
  compressing?: boolean;
};

/**
 * Compact context-size indicator rendered under the composer. Shows how full
 * the active conversation's context window is, so the user can judge whether a
 * follow-up request will fit or a compact is warranted.
 *
 * The "сжать" button triggers on-demand compression of the active chat's
 * context (summarize old turns). Disabled when no handler is wired, while a
 * compression is in-flight, or when the chat is read-only.
 */
export function ContextSizeBar({ usage, onCompress, compressing = false }: ContextSizeBarProps) {
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
      <button
        className="context-size-compact"
        disabled={!onCompress || compressing}
        onClick={onCompress}
        title={compressing ? "Сжатие…" : "Сжать контекст"}
      >
        {compressing ? "сжимаю…" : "сжать"}
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
