import { ArrowLeft, Eye, Save, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { getAgentContext, getAgentContextPreview, saveAgentContext } from "../api";
import { AGENT_CONTEXT_LABEL } from "../i18n/ru";
import type { AgentContextPayload } from "../types";

export type AgentContextViewProps = {
  csrf: string;
  onBack: () => void;
};

const TONE_OPTIONS: { value: AgentContextPayload["tone"]; label: string }[] = [
  { value: "default", label: "Обычный" },
  { value: "concise", label: "Краткий" },
  { value: "detailed", label: "Подробный" }
];

/**
 * Agent Context view (Etap 5). Lets the user set personal instructions
 * (additional system prompt) and a response tone. Includes a live preview
 * of the fully assembled system prompt (BE-assembled).
 */
export function AgentContextView({ csrf, onBack }: AgentContextViewProps) {
  const [instructions, setInstructions] = useState("");
  const [tone, setTone] = useState<AgentContextPayload["tone"]>("default");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [preview, setPreview] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);

  useEffect(() => {
    getAgentContext()
      .then((ctx) => {
        setInstructions(ctx.instructions);
        setTone(ctx.tone);
      })
      .catch((error) => console.warn("Failed to load agent context", error))
      .finally(() => setLoading(false));
  }, []);

  function refreshPreview() {
    setPreviewLoading(true);
    getAgentContextPreview()
      .then((result) => setPreview(result.prompt))
      .catch((error) => console.warn("Failed to load preview", error))
      .finally(() => setPreviewLoading(false));
  }

  useEffect(() => {
    refreshPreview();
  }, []);

  function handleSave() {
    setSaving(true);
    setSaved(false);
    saveAgentContext(csrf, { instructions, tone })
      .then(() => {
        setSaved(true);
        refreshPreview();
        window.setTimeout(() => setSaved(false), 2500);
      })
      .catch((error) => console.warn("Failed to save agent context", error))
      .finally(() => setSaving(false));
  }

  if (loading) {
    return (
      <main className="agent-context-view">
        <div className="extensions-placeholder">Загрузка…</div>
      </main>
    );
  }

  return (
    <main className="agent-context-view">
      <header className="extensions-header">
        <button className="icon-button extensions-back-btn" onClick={onBack} title="Назад к чату">
          <ArrowLeft size={18} />
        </button>
        <h2 className="extensions-title">
          <Sparkles size={18} />
          <span>{AGENT_CONTEXT_LABEL}</span>
        </h2>
      </header>

      <div className="agent-context-body">
        <div className="agent-context-form">
          <label className="agent-context-field">
            <span className="agent-context-label-text">Тон ответов</span>
            <div className="depth-selector" role="group" aria-label="Тон ответов">
              {TONE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  className={`depth-option ${tone === opt.value ? "active" : ""}`}
                  onClick={() => setTone(opt.value)}
                  aria-pressed={tone === opt.value}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </label>

          <label className="agent-context-field">
            <span className="agent-context-label-text">Персональные инструкции</span>
            <textarea
              className="agent-context-textarea"
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              placeholder="Например: «Отвечай на русском. Используй профессиональный тон. Всегда структурируй ответ с заголовками.»"
              rows={8}
              maxLength={10000}
            />
            <span className="agent-context-hint">
              Эти инструкции добавляются в системный промпт агента. Они применяются ко всем
              чатам и режимам.
            </span>
          </label>

          <div className="agent-context-actions">
            <button
              className="primary"
              onClick={handleSave}
              disabled={saving}
            >
              <Save size={15} />
              <span>{saving ? "Сохранение…" : saved ? "Сохранено!" : "Сохранить"}</span>
            </button>
          </div>
        </div>

        <div className="agent-context-preview">
          <div className="agent-context-preview-header">
            <Eye size={15} />
            <span>Предпросмотр системного промпта</span>
            <button
              className="icon-button agent-context-refresh-btn"
              onClick={refreshPreview}
              title="Обновить предпросмотр"
              disabled={previewLoading}
            >
              ↻
            </button>
          </div>
          <pre className="agent-context-preview-text">
            {previewLoading ? "Загрузка…" : preview || "(пусто)"}
          </pre>
        </div>
      </div>
    </main>
  );
}
