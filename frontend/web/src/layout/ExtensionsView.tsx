import { ArrowLeft, Package, RefreshCw } from "lucide-react";
import { useState } from "react";
import { EXTENSIONS_LABEL } from "../i18n/ru";
import type { ExtensionSummary, ExtensionsPayload } from "../types";

export type ExtensionsViewProps = {
  extensions: ExtensionsPayload | null;
  loading: boolean;
  onReload: () => void;
  onBack: () => void;
};

type ExtensionTab = "skills" | "subagents" | "mcp" | "plugins";

const TAB_LABELS: Record<ExtensionTab, string> = {
  skills: "Skills",
  subagents: "Субагенты",
  mcp: "MCP",
  plugins: "Плагины"
};

/**
 * Extensions management view (Etap 4). Read-only listing of loaded extensions
 * (Skills/Subagents/MCP/Plugins) with status badges + manual reload button.
 * Toggle = UI-session only (no BE persistence); per-user disable is future.
 */
export function ExtensionsView({ extensions, loading, onReload, onBack }: ExtensionsViewProps) {
  const [tab, setTab] = useState<ExtensionTab>("skills");
  const [reloading, setReloading] = useState(false);

  function handleReload() {
    setReloading(true);
    onReload();
    // Brief visual feedback; actual reload is async on the BE.
    window.setTimeout(() => setReloading(false), 1500);
  }

  const items: ExtensionSummary[] =
    extensions ? extensions[tab] : [];

  return (
    <main className="extensions-view">
      <header className="extensions-header">
        <button className="icon-button extensions-back-btn" onClick={onBack} title="Назад к чату">
          <ArrowLeft size={18} />
        </button>
        <h2 className="extensions-title">
          <Package size={18} />
          <span>{EXTENSIONS_LABEL}</span>
        </h2>
        <button
          className="extensions-reload-btn"
          onClick={handleReload}
          disabled={reloading}
          title="Перезагрузить расширения"
        >
          <RefreshCw size={15} className={reloading ? "spin" : ""} />
          <span>{reloading ? "Перезагрузка…" : "Обновить"}</span>
        </button>
      </header>

      <div className="extensions-tabs" role="tablist">
        {(Object.keys(TAB_LABELS) as ExtensionTab[]).map((key) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={`extensions-tab ${tab === key ? "active" : ""}`}
            onClick={() => setTab(key)}
          >
            {TAB_LABELS[key]}
          </button>
        ))}
      </div>

      <div className="extensions-body">
        {loading ? (
          <div className="extensions-placeholder">Загрузка…</div>
        ) : items.length === 0 ? (
          <div className="extensions-placeholder">Нет загруженных расширений.</div>
        ) : (
          <div className="extension-grid">
            {items.map((item) => (
              <ExtensionCard key={`${tab}-${item.id}`} item={item} />
            ))}
          </div>
        )}
      </div>
    </main>
  );
}

function ExtensionCard({ item }: { item: ExtensionSummary }) {
  const statusClass =
    item.status === "connected" || item.status === "loaded"
      ? "ok"
      : item.status === "disconnected"
        ? "error"
        : "warning";
  const extraTags: string[] = [];
  if (item.always) extraTags.push("always");
  if (item.type) extraTags.push(item.type);
  if (item.keywords && item.keywords.length > 0) extraTags.push(`${item.keywords.length} keywords`);
  if (item.capabilities && item.capabilities.length > 0)
    extraTags.push(`${item.capabilities.length} caps`);
  if (item.tools && item.tools.length > 0) extraTags.push(`${item.tools.length} tools`);

  return (
    <div className="extension-card">
      <div className="extension-card-header">
        <span className="extension-card-name">{item.name}</span>
        {item.version && <span className="extension-card-version">v{item.version}</span>}
      </div>
      {item.description && <p className="extension-card-desc">{item.description}</p>}
      <div className="extension-card-footer">
        <span className={`extension-status-badge ${statusClass}`}>{item.status}</span>
        {extraTags.length > 0 && (
          <span className="extension-card-tags">{extraTags.join(" · ")}</span>
        )}
      </div>
      {item.tools && item.tools.length > 0 && (
        <div className="extension-tools-list">
          {item.tools.slice(0, 6).map((tool) => (
            <span key={tool} className="extension-tool-chip">
              {tool}
            </span>
          ))}
          {item.tools.length > 6 && <span className="extension-tool-chip">+{item.tools.length - 6}</span>}
        </div>
      )}
    </div>
  );
}
