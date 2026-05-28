import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import { useMemo, useState } from "react";
import type { PanelLayoutState } from "../types";

const STORAGE_KEY = "corpclaw.web.panelLayout";
const HANDLE_WIDTH = 6;
const FILES_MIN = 280;
const FILES_MAX = 680;
const PREVIEW_MIN = 360;
const PREVIEW_MAX = 900;
const MAIN_MIN = 560;
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
      filesWidth: clamp(Number(parsed.filesWidth) || DEFAULT_LAYOUT.filesWidth, FILES_MIN, FILES_MAX),
      previewWidth: clamp(
        Number(parsed.previewWidth) || DEFAULT_LAYOUT.previewWidth,
        PREVIEW_MIN,
        PREVIEW_MAX
      )
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

  function viewportWidth(): number {
    return window.innerWidth || document.documentElement.clientWidth;
  }

  function handlesWidth(options: { filesOpen: boolean; previewOpen: boolean }): number {
    return (options.filesOpen ? HANDLE_WIDTH : 0) + (options.previewOpen ? HANDLE_WIDTH : 0);
  }

  function panelMax(
    panel: "files" | "preview",
    base: PanelLayoutState,
    options: { filesOpen: boolean; previewOpen: boolean }
  ): number {
    const peerWidth =
      panel === "files"
        ? options.previewOpen
          ? base.previewWidth
          : 0
        : options.filesOpen
          ? base.filesWidth
          : 0;
    const hardMax = panel === "files" ? FILES_MAX : PREVIEW_MAX;
    const min = panel === "files" ? FILES_MIN : PREVIEW_MIN;
    const available = viewportWidth() - MAIN_MIN - handlesWidth(options) - peerWidth;
    return Math.max(min, Math.min(hardMax, available));
  }

  function prepareSidePreview(filesOpen: boolean): void {
    setLayout((current) => {
      const next = {
        previewWidth: PREVIEW_MIN,
        filesWidth: current.filesWidth
      };
      if (filesOpen) {
        next.filesWidth = clamp(
          current.filesWidth,
          FILES_MIN,
          panelMax("files", next, { filesOpen: true, previewOpen: true })
        );
      }
      saveLayout(next);
      return next;
    });
  }

  function startResize(
    panel: "files" | "preview",
    event: ReactPointerEvent<HTMLDivElement>,
    options: { filesOpen: boolean; previewOpen: boolean }
  ) {
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
              filesWidth: clamp(
                start.filesWidth + delta,
                FILES_MIN,
                panelMax("files", start, options)
              )
            }
          : {
              ...start,
              previewWidth: clamp(
                start.previewWidth - delta,
                PREVIEW_MIN,
                panelMax("preview", start, options)
              )
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

  return { layout, cssVars, prepareSidePreview, startResize };
}
