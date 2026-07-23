import { useEffect } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { FilePreview } from "../files/FilePreview";
import type { PreviewOverlayMode, PreviewPayload } from "../types";

export type PreviewOverlayProps = {
  preview: PreviewPayload;
  mode: PreviewOverlayMode;
  onModeChange: (mode: PreviewOverlayMode) => void;
  onClose: () => void;
  onStartResize: (event: ReactPointerEvent<HTMLDivElement>) => void;
};

/**
 * Right-side overlay panel hosting a file preview.
 *
 * - `side` mode: absolute panel pinned to the right edge, resizable via its left edge.
 * - `expanded` mode: fullscreen modal with a backdrop.
 *
 * The overlay wrapper owns positioning (`.preview-overlay` / `.preview-overlay.fullscreen`).
 * The inner <FilePreview> receives the real overlay `mode` so its expand/collapse
 * button shows the correct icon/title; in `expanded`, FilePreview's own
 * `.preview-drawer.expanded` (inset:18px) just adds breathing room inside the
 * fullscreen overlay (inset:0) — harmless, not a positioning conflict.
 */
export function PreviewOverlay({
  preview,
  mode,
  onModeChange,
  onClose,
  onStartResize
}: PreviewOverlayProps) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      // Ignore Esc while the user is typing in the composer textarea — closing
      // the preview on Esc-to-clear-IME would be surprising.
      const active = document.activeElement;
      if (active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement) {
        return;
      }
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <>
      {mode === "expanded" && (
        <div className="preview-overlay-backdrop" onClick={onClose} aria-hidden="true" />
      )}
      <div
        className={`preview-overlay ${mode === "expanded" ? "fullscreen" : ""}`}
        role="dialog"
        aria-label="Просмотр файла"
      >
        {mode === "side" && (
          <div
            className="resize-handle preview-overlay-resize"
            onPointerDown={onStartResize}
            role="separator"
            aria-orientation="vertical"
          />
        )}
        <FilePreview
          preview={preview}
          mode={mode}
          onModeChange={() => onModeChange(mode === "expanded" ? "side" : "expanded")}
          onClose={onClose}
        />
      </div>
    </>
  );
}
