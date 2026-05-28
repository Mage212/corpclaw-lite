import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import { useMemo, useState } from "react";
import type { PanelLayoutState } from "../types";

const STORAGE_KEY = "corpclaw.web.panelLayout";
const DEFAULT_LAYOUT: PanelLayoutState = {
  filesWidth: 420,
  previewWidth: 560
};

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function loadLayout(): PanelLayoutState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_LAYOUT;
    const parsed = JSON.parse(raw) as Partial<PanelLayoutState>;
    return {
      filesWidth: clamp(Number(parsed.filesWidth) || DEFAULT_LAYOUT.filesWidth, 280, 680),
      previewWidth: clamp(Number(parsed.previewWidth) || DEFAULT_LAYOUT.previewWidth, 360, 900)
    };
  } catch {
    return DEFAULT_LAYOUT;
  }
}

function saveLayout(layout: PanelLayoutState): void {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(layout));
}

export function useResizablePanels() {
  const [layout, setLayout] = useState<PanelLayoutState>(() => loadLayout());

  const cssVars = useMemo(
    () =>
      ({
        "--files-width": `${layout.filesWidth}px`,
        "--preview-width": `${layout.previewWidth}px`
      }) as CSSProperties,
    [layout]
  );

  function startResize(panel: "files" | "preview", event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const start = layout;
    document.body.classList.add("is-resizing");

    function onMove(pointerEvent: PointerEvent) {
      const delta = pointerEvent.clientX - startX;
      const next =
        panel === "files"
          ? {
              ...start,
              filesWidth: clamp(start.filesWidth + delta, 280, 680)
            }
          : {
              ...start,
              previewWidth: clamp(start.previewWidth - delta, 360, 900)
            };
      setLayout(next);
      saveLayout(next);
    }

    function onUp() {
      document.body.classList.remove("is-resizing");
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
  }

  return { layout, cssVars, startResize };
}
