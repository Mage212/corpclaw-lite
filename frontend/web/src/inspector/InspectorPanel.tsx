import {
  Activity,
  Bot,
  CheckCircle2,
  Clock3,
  Download,
  Eye,
  FileText,
  FolderOpen,
  RotateCw,
  ShieldCheck,
  Wrench,
  X
} from "lucide-react";
import { FilePreview } from "../files/FilePreview";
import { fileIcon, formatSize } from "../files/fileUtils";
import { fileEntryKindLabel, NO_DATA_LABEL, statusPhaseLabel } from "../i18n/ru";
import type {
  ApprovalRequest,
  ContextUsage,
  FileEntry,
  InspectorTab,
  PreviewMode,
  PreviewPayload,
  RunTimelineEvent,
  StatusLine,
  WorkspaceOverviewPayload
} from "../types";

type InspectorPanelProps = {
  activeTab: InspectorTab;
  onTabChange: (tab: InspectorTab) => void;
  overview: WorkspaceOverviewPayload | null;
  overviewLoading: boolean;
  status: StatusLine;
  runEvents: RunTimelineEvent[];
  approvals: ApprovalRequest[];
  contextUsage: ContextUsage | null;
  preview: PreviewPayload | null;
  previewMode: PreviewMode;
  onPreviewModeChange: (mode: PreviewMode) => void;
  onClose: () => void;
  onClosePreview: () => void;
  onRefreshOverview: () => void;
  onPreviewPath: (path: string) => void;
  onAnswerApproval: (approvalId: string, approved: boolean) => void;
};

export function InspectorPanel({
  activeTab,
  onTabChange,
  overview,
  overviewLoading,
  status,
  runEvents,
  approvals,
  contextUsage,
  preview,
  previewMode,
  onPreviewModeChange,
  onClose,
  onClosePreview,
  onRefreshOverview,
  onPreviewPath,
  onAnswerApproval
}: InspectorPanelProps) {
  return (
    <aside className="inspector-panel">
      <header className="inspector-header">
        <div className="inspector-header-main">
          <strong>Операционный центр</strong>
          <span>{status.active ? status.label : "Операционный обзор"}</span>
        </div>
        <div className="inspector-header-actions">
          <button
            className="icon-button"
            onClick={onRefreshOverview}
            disabled={overviewLoading}
            title="Обновить"
          >
            <RotateCw size={17} />
          </button>
          <button className="icon-button" onClick={onClose} title="Скрыть операционный центр">
            <X size={17} />
          </button>
        </div>
      </header>
      <div className="inspector-tabs" role="tablist">
        <button
          className={activeTab === "overview" ? "active" : ""}
          onClick={() => onTabChange("overview")}
        >
          Обзор
        </button>
        <button
          className={activeTab === "run" ? "active" : ""}
          onClick={() => onTabChange("run")}
        >
          Выполнение
        </button>
        <button
          className={activeTab === "preview" ? "active" : ""}
          onClick={() => onTabChange("preview")}
          disabled={!preview}
        >
          Файл
        </button>
      </div>
      <div className="inspector-body">
        {activeTab === "overview" && (
          <OverviewTab
            overview={overview}
            loading={overviewLoading}
            contextUsage={contextUsage}
            onPreviewPath={onPreviewPath}
          />
        )}
        {activeTab === "run" && (
          <RunTab
            status={status}
            runEvents={runEvents}
            approvals={approvals}
            onAnswerApproval={onAnswerApproval}
          />
        )}
        {activeTab === "preview" && (
          <PreviewTab
            preview={preview}
            previewMode={previewMode}
            onPreviewModeChange={onPreviewModeChange}
            onClosePreview={onClosePreview}
          />
        )}
      </div>
    </aside>
  );
}

function OverviewTab({
  overview,
  loading,
  contextUsage,
  onPreviewPath
}: {
  overview: WorkspaceOverviewPayload | null;
  loading: boolean;
  contextUsage: ContextUsage | null;
  onPreviewPath: (path: string) => void;
}) {
  const latest = contextUsage?.latest_total_tokens ?? 0;
  const limit = contextUsage?.context_limit_tokens ?? 0;
  const percent = Math.round((contextUsage?.context_ratio ?? 0) * 100);
  return (
    <div className="inspector-stack">
      <section className="ops-summary">
        <div className="ops-summary-main">
          <span className="summary-icon">
            <Bot size={20} />
          </span>
          <div>
            <strong>{overview?.user.name || "CorpClaw Lite"}</strong>
            <span>{overview?.user.department || "рабочая область"}</span>
          </div>
        </div>
        <div className="summary-grid">
          <Metric label="LLM" value={overview?.llm.model || "не выбран"} />
          <Metric label="Провайдер" value={overview?.llm.provider || NO_DATA_LABEL} />
          <Metric label="Контекст" value={limit ? `${latest} / ${limit}` : NO_DATA_LABEL} />
          <Metric label="Заполнение" value={limit ? `${percent}%` : NO_DATA_LABEL} />
        </div>
      </section>

      <section className="inspector-section">
        <div className="section-heading">
          <span>Последние результаты</span>
          {loading && <small>обновляю</small>}
        </div>
        {overview?.recent_outputs.length ? (
          <div className="artifact-list">
            {overview.recent_outputs.map((item) => (
              <div className="artifact-row" key={`${item.name}_${item.created_at}`}>
                <FileText size={16} />
                <div>
                  <strong title={item.name}>{item.name}</strong>
                  <span title={item.caption || item.path || ""}>
                    {item.caption || item.path || "Файл"}
                  </span>
                </div>
                <div className="artifact-actions">
                  {item.path && (
                    <button onClick={() => onPreviewPath(item.path || "")} title="Просмотр">
                      <Eye size={15} />
                    </button>
                  )}
                  {item.url && (
                    <a href={item.url} download={item.name} title="Скачать">
                      <Download size={15} />
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyLine label="Артефакты появятся после выполнения задач." />
        )}
      </section>

      <section className="inspector-section">
        <div className="section-heading">
          <span>Последние файлы</span>
        </div>
        {overview?.recent_files.length ? (
          <div className="recent-file-list">
            {overview.recent_files.map((entry) => (
              <RecentFile key={entry.path} entry={entry} onPreviewPath={onPreviewPath} />
            ))}
          </div>
        ) : (
          <EmptyLine label="В рабочей области пока нет файлов." />
        )}
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong title={value}>{value}</strong>
    </div>
  );
}

function RecentFile({
  entry,
  onPreviewPath
}: {
  entry: FileEntry;
  onPreviewPath: (path: string) => void;
}) {
  return (
    <button className="recent-file-row" onClick={() => onPreviewPath(entry.path)} title={entry.path}>
      <span>{fileIcon(entry)}</span>
      <div>
        <strong>{entry.name}</strong>
        <small>
          {entry.is_dir ? fileEntryKindLabel(entry) : formatSize(entry.size_bytes)} ·{" "}
          {entry.modified_at}
        </small>
      </div>
    </button>
  );
}

function RunTab({
  status,
  runEvents,
  approvals,
  onAnswerApproval
}: {
  status: StatusLine;
  runEvents: RunTimelineEvent[];
  approvals: ApprovalRequest[];
  onAnswerApproval: (approvalId: string, approved: boolean) => void;
}) {
  return (
    <div className="inspector-stack">
      <section className={`run-state ${status.tone}`}>
        <div>
          <Activity size={18} />
          <strong>{status.active ? status.label : "Готов к задаче"}</strong>
        </div>
        <span>{statusPhaseLabel(status.active ? status.phase : "idle")}</span>
      </section>

      {approvals.length > 0 && (
        <section className="inspector-section">
          <div className="section-heading">
            <span>Подтверждения</span>
          </div>
          {approvals.map((approval) => (
            <div className="approval-inline" key={approval.approval_id}>
              <strong>{approval.action}</strong>
              <p>{approval.details}</p>
              <div>
                <button
                  className="primary"
                  onClick={() => onAnswerApproval(approval.approval_id, true)}
                >
                  Разрешить
                </button>
                <button onClick={() => onAnswerApproval(approval.approval_id, false)}>
                  Отклонить
                </button>
              </div>
            </div>
          ))}
        </section>
      )}

      <section className="inspector-section">
        <div className="section-heading">
          <span>Ход выполнения</span>
        </div>
        {runEvents.length ? (
          <div className="run-timeline">
            {runEvents.map((event) => (
              <TimelineRow key={event.id} event={event} />
            ))}
          </div>
        ) : (
          <EmptyLine label="События запуска появятся во время выполнения задачи." />
        )}
      </section>
    </div>
  );
}

function TimelineRow({ event }: { event: RunTimelineEvent }) {
  const icon =
    event.type === "tool" ? (
      <Wrench size={15} />
    ) : event.type === "approval" ? (
      <ShieldCheck size={15} />
    ) : event.type === "file" ? (
      <FolderOpen size={15} />
    ) : event.type === "done" ? (
      <CheckCircle2 size={15} />
    ) : (
      <Clock3 size={15} />
    );
  return (
    <div className={`timeline-row ${event.tone}`}>
      <span className="timeline-icon">{icon}</span>
      <div>
        <strong>{event.label}</strong>
        {event.detail && <span>{event.detail}</span>}
      </div>
      <time>{event.createdAt}</time>
    </div>
  );
}

function PreviewTab({
  preview,
  previewMode,
  onPreviewModeChange,
  onClosePreview
}: {
  preview: PreviewPayload | null;
  previewMode: PreviewMode;
  onPreviewModeChange: (mode: PreviewMode) => void;
  onClosePreview: () => void;
}) {
  if (!preview) {
    return (
      <div className="preview-empty">
        <FileText size={28} />
        <strong>Файл не выбран</strong>
        <span>Откройте файл из рабочей области или карточки результата.</span>
      </div>
    );
  }
  return (
    <div className="embedded-preview-shell">
      <button className="preview-close-inline" onClick={onClosePreview}>
        <X size={15} /> Закрыть файл
      </button>
      <FilePreview
        preview={preview}
        mode={previewMode}
        onModeChange={onPreviewModeChange}
        onClose={onClosePreview}
        embedded
      />
    </div>
  );
}

function EmptyLine({ label }: { label: string }) {
  return <div className="empty-line">{label}</div>;
}
