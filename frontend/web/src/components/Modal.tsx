import type { FormEvent, ReactNode } from "react";
import { X } from "lucide-react";

type ModalProps = {
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  onClose: () => void;
  onSubmit?: () => void | Promise<void>;
};

export function Modal({ title, description, children, footer, onClose, onSubmit }: ModalProps) {
  async function submit(event: FormEvent) {
    event.preventDefault();
    await onSubmit?.();
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <form
        className="modal-card"
        onSubmit={submit}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <strong>{title}</strong>
            {description && <span>{description}</span>}
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Закрыть">
            <X size={18} />
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer className="modal-footer">{footer}</footer>}
      </form>
    </div>
  );
}
