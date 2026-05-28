import { Copy, Download, Maximize2, Minimize2, X, ZoomIn } from "lucide-react";
import { useState } from "react";
import { downloadUrl } from "../api";
import type { PreviewMode, PreviewPayload } from "../types";
import { formatSize } from "./fileUtils";

type FilePreviewProps = {
  preview: PreviewPayload;
  mode: PreviewMode;
  onModeChange: (mode: PreviewMode) => void;
  onClose: () => void;
};

export function FilePreview({ preview, mode, onModeChange, onClose }: FilePreviewProps) {
  const [imageFit, setImageFit] = useState<"fit" | "original">("fit");
  const shellClass = mode === "expanded" ? "preview-drawer expanded" : "preview-drawer";
  const canCopy = preview.type === "text" && !preview.error;

  async function copyText() {
    if (preview.type === "text" && !preview.error) {
      await navigator.clipboard.writeText(preview.content);
    }
  }

  return (
    <aside className={shellClass}>
      <header>
        <div>
          <strong title={preview.entry.name}>{preview.entry.name}</strong>
          <span title={preview.entry.path}>{preview.entry.path}</span>
        </div>
        <div className="preview-actions">
          {preview.type === "image" && (
            <button
              className="icon-button"
              onClick={() => setImageFit((value) => (value === "fit" ? "original" : "fit"))}
              title={imageFit === "fit" ? "Оригинальный размер" : "Вписать"}
            >
              <ZoomIn size={18} />
            </button>
          )}
          {canCopy && (
            <button className="icon-button" onClick={copyText} title="Скопировать текст">
              <Copy size={18} />
            </button>
          )}
          <a className="icon-button" href={downloadUrl(preview.entry.path)} title="Скачать">
            <Download size={18} />
          </a>
          <button
            className="icon-button"
            onClick={() => onModeChange(mode === "side" ? "expanded" : "side")}
            title={mode === "side" ? "Расширить" : "Вернуть сбоку"}
          >
            {mode === "side" ? <Maximize2 size={18} /> : <Minimize2 size={18} />}
          </button>
          <button className="icon-button" onClick={onClose} title="Закрыть">
            <X size={18} />
          </button>
        </div>
      </header>
      <PreviewContent preview={preview} imageFit={imageFit} />
    </aside>
  );
}

function PreviewContent({
  preview,
  imageFit
}: {
  preview: PreviewPayload;
  imageFit: "fit" | "original";
}) {
  if (preview.type === "image") {
    return (
      <div className={`image-preview ${imageFit}`}>
        <img src={preview.url} alt={preview.entry.name} />
      </div>
    );
  }

  if (preview.type === "text") {
    return (
      <div className="text-preview">
        {preview.truncated && <div className="preview-warning">Файл слишком большой для preview.</div>}
        <pre>{preview.error ? preview.error : preview.content}</pre>
      </div>
    );
  }

  return (
    <div className="metadata">
      <dl>
        <div>
          <dt>Тип</dt>
          <dd>{preview.entry.kind}</dd>
        </div>
        <div>
          <dt>Размер</dt>
          <dd>{formatSize(preview.entry.size_bytes)}</dd>
        </div>
        <div>
          <dt>Изменен</dt>
          <dd>{preview.entry.modified_at}</dd>
        </div>
        <div>
          <dt>Путь</dt>
          <dd>{preview.entry.path}</dd>
        </div>
      </dl>
      <a className="primary link-button" href={downloadUrl(preview.entry.path)}>
        <Download size={16} /> Скачать
      </a>
    </div>
  );
}
