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
 * The inner <FilePreview> is always rendered with `mode="side"` so it does not
 * apply its own `position: fixed` styling; the overlay wrapper owns all positioning.
 * FilePreview's expand/collapse button calls back into `onModeChange`, which we
 * translate into overlay-mode (side ↔ expanded).
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
          mode="side"
          onModeChange={() => onModeChange(mode === "expanded" ? "side" : "expanded")}
          onClose={onClose}
        />
      </div>
    </>
  );
}
