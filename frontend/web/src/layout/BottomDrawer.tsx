import { ChevronDown, ChevronUp, FolderClosed } from "lucide-react";
import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { FILES_LABEL } from "../i18n/ru";

export type BottomDrawerProps = {
  open: boolean;
  onToggle: () => void;
  onStartResize: (event: ReactPointerEvent<HTMLDivElement>) => void;
  children: ReactNode;
};

/**
 * Bottom-docked file drawer. Renders an always-visible peek-bar and, when open,
 * a resizable body hosting the file explorer.
 *
 * The resize handle is the drawer's top edge: dragging it up grows the drawer.
 */
export function BottomDrawer({ open, onToggle, onStartResize, children }: BottomDrawerProps) {
  return (
    <div className={`bottom-drawer ${open ? "drawer-open" : ""}`}>
      {open && (
        <div
          className="resize-handle drawer-resize"
          onPointerDown={onStartResize}
          role="separator"
          aria-orientation="horizontal"
        />
      )}
      <div
        className="drawer-peek-bar"
        onClick={onToggle}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onToggle();
          }
        }}
        role="button"
        tabIndex={0}
      >
        <span className="drawer-peek-label">
          <FolderClosed size={15} />
          <span>{FILES_LABEL}</span>
        </span>
        <span className="drawer-peek-toggle">
          {open ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
        </span>
      </div>
      {open && <div className="drawer-body">{children}</div>}
    </div>
  );
}
