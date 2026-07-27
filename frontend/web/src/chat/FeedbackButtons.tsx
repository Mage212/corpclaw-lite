import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";
import { rateMessage } from "../api";

export type FeedbackButtonsProps = {
  csrf: string;
  runId: string;
};

type Rating = "up" | "down" | null;

/**
 * B-121: 👍/👎 on an assistant run. Calls POST /api/feedback with the run_id
 * (the JOIN key against logs/llm_payloads.jsonl). Local state reflects the
 * user's choice immediately and locks the buttons after a successful submit —
 * re-voting is possible server-side via allow_change, but the UI keeps it
 * simple: one tap, persisted, done.
 */
export function FeedbackButtons({ csrf, runId }: FeedbackButtonsProps) {
  const [busy, setBusy] = useState(false);
  const [choice, setChoice] = useState<Rating>(null);
  const [error, setError] = useState<string | null>(null);

  async function vote(rating: "up" | "down") {
    if (busy || choice !== null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await rateMessage(csrf, runId, rating);
      setChoice(result.rating);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить оценку.");
      setBusy(false);
    }
  }

  return (
    <div className="feedback-buttons">
      <button
        type="button"
        className={`feedback-btn ${choice === "up" ? "active" : ""}`}
        disabled={busy || choice !== null}
        onClick={() => vote("up")}
        title="Хороший ответ"
        aria-label="Хороший ответ"
        aria-pressed={choice === "up"}
      >
        <ThumbsUp size={15} />
      </button>
      <button
        type="button"
        className={`feedback-btn ${choice === "down" ? "active" : ""}`}
        disabled={busy || choice !== null}
        onClick={() => vote("down")}
        title="Плохой ответ"
        aria-label="Плохой ответ"
        aria-pressed={choice === "down"}
      >
        <ThumbsDown size={15} />
      </button>
      {error && <span className="feedback-error">{error}</span>}
    </div>
  );
}
