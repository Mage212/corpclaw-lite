import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import { useMemo, useState } from "react";
import { parsePanelLayoutState } from "../contracts";
import type { PanelLayoutState } from "../types";

const STORAGE_KEY = "corpclaw.web.panelLayout";

// Sidebar (left navigation column) — vertical resize handle on its right edge.
const SIDEBAR_MIN = 220;
const SIDEBAR_MAX = 400;
const SIDEBAR_DEFAULT = 280;

// Preview overlay (slide-in panel on the right when not fullscreen).
const PREVIEW_MIN = 320;
const PREVIEW_MAX = 900;
const PREVIEW_DEFAULT = 440;

// Bottom file drawer — horizontal resize handle on its top edge.
// Measured in px; clamped to a viewport-height fraction at resize time.
const DRAWER_MIN_PX = 150;
const DRAWER_DEFAULT_VH = 0.4; // 40% of viewport height
const DRAWER_MAX_VH = 0.75; // 75% of viewport height

const MAIN_MIN = 480;
const HANDLE_WIDTH = 6;

type ResizablePanel = "sidebar" | "preview" | "drawer";

const DEFAULT_LAYOUT: PanelLayoutState = {
  sidebarWidth: SIDEBAR_DEFAULT,
  previewWidth: PREVIEW_DEFAULT,
  drawerHeight: Math.round(DRAWER_DEFAULT_VH * 720) // assumes ~720px tall viewport fallback
};

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function drawerMax(): number {
  return Math.max(DRAWER_MIN_PX, Math.floor((window.innerHeight || 720) * DRAWER_MAX_VH));
}

function loadLayout(): PanelLayoutState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_LAYOUT;
    const parsed: unknown = JSON.parse(raw);
    const layout = parsePanelLayoutState(parsed);
    if (!layout) return DEFAULT_LAYOUT;
    const sidebarWidth = clamp(layout.sidebarWidth, SIDEBAR_MIN, SIDEBAR_MAX);
    const previewMax = Math.max(
      PREVIEW_MIN,
      Math.min(PREVIEW_MAX, window.innerWidth - sidebarWidth - MAIN_MIN - HANDLE_WIDTH)
    );
    const drawerHeight =
      layout.drawerHeight === null
        ? null
        : clamp(layout.drawerHeight, DRAWER_MIN_PX, drawerMax());
    return {
      sidebarWidth,
      previewWidth: clamp(layout.previewWidth, PREVIEW_MIN, previewMax),
      drawerHeight
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
        "--sidebar-width": `${layout.sidebarWidth}px`,
        "--preview-overlay-width": `${layout.previewWidth}px`,
        "--drawer-height":
          layout.drawerHeight === null ? "auto" : `${layout.drawerHeight}px`
      }) as CSSProperties,
    [layout]
  );

  function sidebarMax(): number {
    // Sidebar and preview-overlay don't share the grid row, but keep both within viewport.
    const reserved = MAIN_MIN + (layout.previewWidth > 0 ? HANDLE_WIDTH : 0);
    return Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, window.innerWidth - reserved));
  }

  function previewMax(): number {
    const reserved = layout.sidebarWidth + MAIN_MIN + HANDLE_WIDTH;
    return Math.max(PREVIEW_MIN, Math.min(PREVIEW_MAX, window.innerWidth - reserved));
  }

  /**
   * Begin a pointer-driven resize for the given panel.
   * - `sidebar`/`preview`: drag horizontally (X delta). Sidebar grows rightward,
   *   preview-overlay grows leftward (drag left → wider).
   * - `drawer`: drag vertically (Y delta). Drawer grows upward (drag up → taller).
   */
  function startResize(
    panel: ResizablePanel,
    event: ReactPointerEvent<HTMLDivElement>
  ) {
    event.preventDefault();
    const startX = event.clientX;
    const startY = event.clientY;
    const start = layout;
    document.body.classList.add("is-resizing");

    function onMove(pointerEvent: PointerEvent) {
      setLayout((current) => {
        let next: PanelLayoutState;
        if (panel === "sidebar") {
          const delta = pointerEvent.clientX - startX;
          next = {
            ...current,
            sidebarWidth: clamp(start.sidebarWidth + delta, SIDEBAR_MIN, sidebarMax())
          };
        } else if (panel === "preview") {
          const delta = pointerEvent.clientX - startX;
          next = {
            ...current,
            previewWidth: clamp(start.previewWidth - delta, PREVIEW_MIN, previewMax())
          };
        } else {
          // drawer: dragging the top edge up (negative Y delta) increases height
          const delta = pointerEvent.clientY - startY;
          const currentHeight = start.drawerHeight ?? DRAWER_MIN_PX;
          next = {
            ...current,
            drawerHeight: clamp(currentHeight - delta, DRAWER_MIN_PX, drawerMax())
          };
        }
        saveLayout(next);
        return next;
      });
    }

    function onUp() {
      document.body.classList.remove("is-resizing");
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
  }

  /** Explicitly set drawer height (e.g. from open/close toggles). `null` = collapsed. */
  function setDrawerHeight(height: number | null): void {
    setLayout((current) => {
      const drawerHeight =
        height === null
          ? null
          : clamp(height, DRAWER_MIN_PX, drawerMax());
      const next = { ...current, drawerHeight };
      saveLayout(next);
      return next;
    });
  }

  return { layout, cssVars, startResize, setDrawerHeight };
}
