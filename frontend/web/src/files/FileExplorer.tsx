import {
  ChevronDown,
  ChevronRight,
  ChevronsUp,
  Copy,
  Folder,
  FolderPlus,
  Grid3X3,
  List,
  Maximize2,
  MoreVertical,
  Minimize2,
  MoveRight,
  PanelLeftOpen,
  RefreshCw,
  Search,
  TableProperties,
  Trash2,
  Upload,
  X
} from "lucide-react";
import type { DragEvent, FormEvent, MouseEvent } from "react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  copyFiles,
  deleteFiles,
  downloadUrl,
  listFiles,
  loadTree,
  makeDirectory,
  moveFiles,
  previewFile,
  renameFile,
  searchFiles,
  uploadFiles
} from "../api";
import { Modal } from "../components/Modal";
import { parseDraggedPaths } from "../contracts";
import type {
  DirectoryPayload,
  FileEntry,
  FileExplorerMode,
  PreviewMode,
  PreviewPayload,
  TreeNode,
  UploadItem,
  ViewMode
} from "../types";
import { fileIcon, flattenFolders, formatSize, parentPath, pathAncestors } from "./fileUtils";

type FileExplorerProps = {
  csrf: string;
  open: boolean;
  mode: FileExplorerMode;
  onModeChange: (mode: FileExplorerMode) => void;
  onPreview: (preview: PreviewPayload, mode: PreviewMode) => void;
  onWorkspaceChanged?: () => void;
};

type FileAction =
  | { type: "mkdir" }
  | { type: "rename"; entry: FileEntry }
  | { type: "move"; paths: string[] }
  | { type: "copy"; paths: string[] }
  | { type: "delete"; paths: string[] };

function id(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2)}_${Date.now()}`;
}

const CONTEXT_MENU_VIEWPORT_MARGIN = 8;

export function FileExplorer({
  csrf,
  open,
  mode,
  onModeChange,
  onPreview,
  onWorkspaceChanged
}: FileExplorerProps) {
  const [cwd, setCwd] = useState("");
  const [directory, setDirectory] = useState<DirectoryPayload>({ path: "", entries: [] });
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set([""]));
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [viewMode, setViewMode] = useState<ViewMode>("details");
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<FileEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [dropActive, setDropActive] = useState(false);
  const [foldersDrawerOpen, setFoldersDrawerOpen] = useState(false);
  const [context, setContext] = useState<{ x: number; y: number; entry: FileEntry } | null>(null);
  const [action, setAction] = useState<FileAction | null>(null);
  const entries = query.trim() ? searchResults : directory.entries;

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const [listing, loadedTree] = await Promise.all([listFiles(cwd), loadTree()]);
      setDirectory(listing);
      setTree(loadedTree);
    } finally {
      setBusy(false);
    }
  }, [cwd]);

  useEffect(() => {
    refresh().catch(console.error);
  }, [refresh]);

  useEffect(() => {
    setExpanded((current) => new Set([...current, "", ...pathAncestors(cwd)]));
  }, [cwd]);

  useEffect(() => {
    if (!foldersDrawerOpen) return;

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setFoldersDrawerOpen(false);
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [foldersDrawerOpen]);

  useEffect(() => {
    const value = query.trim();
    if (!value) {
      setSearchResults([]);
      return;
    }
    const timer = window.setTimeout(() => {
      searchFiles(value)
        .then((payload) => setSearchResults(payload.entries))
        .catch((error: unknown) => {
          console.error(error);
          setSearchResults([]);
        });
    }, 220);
    return () => window.clearTimeout(timer);
  }, [query]);

  async function upload(fileList: FileList | File[], targetDir = cwd) {
    const files = Array.from(fileList);
    if (!files.length) return;
    setUploads((items) => [
      ...items,
      ...files.map((file) => ({
        id: id("upload"),
        name: file.name,
        progress: 0,
        status: "queued" as const
      }))
    ]);
    try {
      await uploadFiles(csrf, targetDir, files, (fileName, progress) => {
        setUploads((items) =>
          items.map((item) =>
            item.name === fileName ? { ...item, progress, status: "uploading" } : item
          )
        );
      });
      setUploads((items) =>
        items.map((item) =>
          files.some((file) => file.name === item.name)
            ? { ...item, progress: 100, status: "done" }
            : item
        )
      );
      await refresh();
      onWorkspaceChanged?.();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Upload failed";
      setUploads((items) =>
        items.map((item) =>
          files.some((file) => file.name === item.name)
            ? { ...item, status: "error", error: message }
            : item
        )
      );
    }
  }

  async function openEntry(entry: FileEntry) {
    if (entry.is_dir) {
      navigate(entry.path);
      return;
    }
    await openFilePreview(entry, "side");
  }

  async function openFilePreview(entry: FileEntry, nextMode: PreviewMode) {
    if (entry.is_dir) return;
    onPreview(await previewFile(entry.path), nextMode);
  }

  function selectedPaths(entry?: FileEntry): string[] {
    if (entry && selected.size === 0) return [entry.path];
    if (entry && !selected.has(entry.path)) return [entry.path];
    return Array.from(selected);
  }

  function navigate(path: string) {
    setCwd(path);
    setSelected(new Set());
    setQuery("");
    setFoldersDrawerOpen(false);
  }

  function toggleSelected(entry: FileEntry, multi: boolean) {
    setSelected((current) => {
      const next = multi ? new Set(current) : new Set<string>();
      if (next.has(entry.path)) next.delete(entry.path);
      else next.add(entry.path);
      return next;
    });
  }

  function openContext(event: MouseEvent, entry: FileEntry) {
    event.preventDefault();
    event.stopPropagation();
    setContext({ x: event.clientX, y: event.clientY, entry });
  }

  async function moveDragged(event: DragEvent, targetDir: string) {
    event.preventDefault();
    setDropActive(false);
    const dragged = event.dataTransfer.getData("application/x-corpclaw-paths");
    if (dragged) {
      const paths = parseDraggedPaths(dragged);
      if (!paths.length) return;
      await moveFiles(csrf, paths, targetDir);
      setSelected(new Set());
      await refresh();
      onWorkspaceChanged?.();
    } else if (event.dataTransfer.files.length) {
      await upload(event.dataTransfer.files, targetDir);
    }
  }

  async function handleDrop(event: DragEvent, targetDir = cwd) {
    event.preventDefault();
    setDropActive(false);
    await moveDragged(event, targetDir);
  }

  async function runFileAction(next: FileAction, payload: { name?: string; target?: string } = {}) {
    if (next.type === "mkdir") {
      await makeDirectory(csrf, cwd, payload.name || "");
    } else if (next.type === "rename") {
      await renameFile(csrf, next.entry.path, payload.name || "");
    } else if (next.type === "move") {
      await moveFiles(csrf, next.paths, payload.target || "");
      setSelected(new Set());
    } else if (next.type === "copy") {
      await copyFiles(csrf, next.paths, payload.target || "");
      setSelected(new Set());
    } else if (next.type === "delete") {
      await deleteFiles(csrf, next.paths, true);
      setSelected(new Set());
    }
    setAction(null);
    await refresh();
    onWorkspaceChanged?.();
  }

  if (!open) {
    return null;
  }

  return (
    <aside
      className={`file-explorer ${mode === "expanded" ? "expanded" : ""} ${
        dropActive ? "drop-active" : ""
      }`}
      onDragOver={(event) => {
        event.preventDefault();
        setDropActive(true);
      }}
      onDragLeave={() => setDropActive(false)}
      onDrop={(event) => handleDrop(event)}
    >
      <header className="files-header">
        <div>
          <strong>Workspace</strong>
          <span title={cwd || "root"}>{cwd || "root"}</span>
        </div>
        <div className="files-header-actions">
          <button
            className="icon-button"
            onClick={() => onModeChange(mode === "side" ? "expanded" : "side")}
            title={mode === "side" ? "Открыть на весь экран" : "Вернуть сбоку"}
          >
            {mode === "side" ? <Maximize2 size={18} /> : <Minimize2 size={18} />}
          </button>
        </div>
      </header>

      <div className="file-toolbar">
        <button className="icon-button" onClick={refresh} disabled={busy} title="Обновить">
          <RefreshCw size={17} />
        </button>
        <button className="icon-button" onClick={() => setAction({ type: "mkdir" })} title="Папка">
          <FolderPlus size={17} />
        </button>
        <label className="icon-button file-picker" title="Загрузить">
          <Upload size={17} />
          <input
            multiple
            type="file"
            onChange={(event) => event.target.files && upload(event.target.files)}
          />
        </label>
        <button
          className="folder-tree-toggle"
          onClick={() => setFoldersDrawerOpen(true)}
          title="Папки"
        >
          <PanelLeftOpen size={17} />
          <span>Папки</span>
        </button>
        <ViewModeButtons mode={viewMode} onModeChange={setViewMode} />
      </div>

      <div className="search-box">
        <Search size={15} />
        <input
          value={query}
          placeholder="Поиск файлов"
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      <Breadcrumbs path={cwd} onNavigate={navigate} />

      <div className="file-manager-body">
        <FileTree
          node={tree}
          current={cwd}
          expanded={expanded}
          onToggleExpanded={(path) =>
            setExpanded((current) => {
              const next = new Set(current);
              if (next.has(path)) next.delete(path);
              else next.add(path);
              return next;
            })
          }
          onNavigate={navigate}
          onDropToFolder={moveDragged}
        />
        <div className="file-main">
          <SelectionBar
            selectedCount={selected.size}
            onCopy={() => setAction({ type: "copy", paths: Array.from(selected) })}
            onMove={() => setAction({ type: "move", paths: Array.from(selected) })}
            onDelete={() => setAction({ type: "delete", paths: Array.from(selected) })}
          />
          <FileList
            cwd={cwd}
            entries={entries}
            query={query}
            mode={viewMode}
            selected={selected}
            onNavigateUp={() => navigate(parentPath(cwd))}
            onOpen={openEntry}
            onSelect={toggleSelected}
            onContext={openContext}
            onDragPaths={selectedPaths}
            onDropToFolder={(event, targetDir) => handleDrop(event, targetDir)}
          />
        </div>
      </div>

      {foldersDrawerOpen && (
        <div className="folders-drawer" role="dialog" aria-label="Папки">
          <button
            className="folders-drawer-backdrop"
            onClick={() => setFoldersDrawerOpen(false)}
            aria-label="Закрыть папки"
          />
          <div className="folders-drawer-panel">
            <header className="folders-drawer-header">
              <strong>Папки</strong>
              <button
                className="icon-button"
                onClick={() => setFoldersDrawerOpen(false)}
                title="Закрыть"
              >
                <X size={17} />
              </button>
            </header>
            <FileTree
              node={tree}
              current={cwd}
              expanded={expanded}
              onToggleExpanded={(path) =>
                setExpanded((current) => {
                  const next = new Set(current);
                  if (next.has(path)) next.delete(path);
                  else next.add(path);
                  return next;
                })
              }
              onNavigate={navigate}
              onDropToFolder={moveDragged}
            />
          </div>
        </div>
      )}

      <UploadQueue uploads={uploads} />

      {context && (
        <ContextMenu
          context={context}
          onClose={() => setContext(null)}
          onOpen={() => openEntry(context.entry)}
          onPreview={() => openFilePreview(context.entry, "side")}
          onFullPreview={() => openFilePreview(context.entry, "expanded")}
          onRename={() => setAction({ type: "rename", entry: context.entry })}
          onCopy={() => setAction({ type: "copy", paths: selectedPaths(context.entry) })}
          onMove={() => setAction({ type: "move", paths: selectedPaths(context.entry) })}
          onDelete={() => setAction({ type: "delete", paths: selectedPaths(context.entry) })}
        />
      )}

      {action && (
        <FileActionDialog
          action={action}
          cwd={cwd}
          tree={tree}
          onClose={() => setAction(null)}
          onSubmit={runFileAction}
        />
      )}
    </aside>
  );
}

function ViewModeButtons({
  mode,
  onModeChange
}: {
  mode: ViewMode;
  onModeChange: (mode: ViewMode) => void;
}) {
  return (
    <div className="view-toggle" aria-label="Режим отображения">
      <button
        className={mode === "details" ? "active" : ""}
        onClick={() => onModeChange("details")}
        title="Таблица"
      >
        <TableProperties size={16} />
      </button>
      <button
        className={mode === "list" ? "active" : ""}
        onClick={() => onModeChange("list")}
        title="Список"
      >
        <List size={16} />
      </button>
      <button
        className={mode === "grid" ? "active" : ""}
        onClick={() => onModeChange("grid")}
        title="Плитка"
      >
        <Grid3X3 size={16} />
      </button>
    </div>
  );
}

function Breadcrumbs({ path, onNavigate }: { path: string; onNavigate: (path: string) => void }) {
  const parts = path.split("/").filter(Boolean);
  const crumbs = parts.map((part, index) => ({
    label: part,
    path: parts.slice(0, index + 1).join("/")
  }));
  return (
    <nav className="breadcrumbs">
      <button onClick={() => onNavigate("")}>root</button>
      {crumbs.map((crumb) => (
        <button key={crumb.path} onClick={() => onNavigate(crumb.path)} title={crumb.path}>
          / {crumb.label}
        </button>
      ))}
    </nav>
  );
}

function FileTree({
  node,
  current,
  expanded,
  onToggleExpanded,
  onNavigate,
  onDropToFolder
}: {
  node: TreeNode | null;
  current: string;
  expanded: Set<string>;
  onToggleExpanded: (path: string) => void;
  onNavigate: (path: string) => void;
  onDropToFolder: (event: DragEvent, targetDir: string) => Promise<void>;
}) {
  return (
    <nav className="file-tree" aria-label="Папки">
      {node ? (
        <TreeRow
          node={node}
          level={0}
          current={current}
          expanded={expanded}
          onToggleExpanded={onToggleExpanded}
          onNavigate={onNavigate}
          onDropToFolder={onDropToFolder}
        />
      ) : (
        <div className="tree-empty">Нет папок</div>
      )}
    </nav>
  );
}

function TreeRow({
  node,
  level,
  current,
  expanded,
  onToggleExpanded,
  onNavigate,
  onDropToFolder
}: {
  node: TreeNode;
  level: number;
  current: string;
  expanded: Set<string>;
  onToggleExpanded: (path: string) => void;
  onNavigate: (path: string) => void;
  onDropToFolder: (event: DragEvent, targetDir: string) => Promise<void>;
}) {
  const children = node.children || [];
  const isExpanded = expanded.has(node.path);
  const active = current === node.path;
  return (
    <div className="tree-group">
      <div
        className={`tree-row ${active ? "active" : ""}`}
        style={{ paddingLeft: `${8 + level * 14}px` }}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => onDropToFolder(event, node.path)}
      >
        <button
          className="tree-caret"
          onClick={() => onToggleExpanded(node.path)}
          disabled={!children.length}
          title={isExpanded ? "Свернуть" : "Развернуть"}
        >
          {children.length ? (
            isExpanded ? (
              <ChevronDown size={14} />
            ) : (
              <ChevronRight size={14} />
            )
          ) : (
            <span />
          )}
        </button>
        <button className="tree-label" onClick={() => onNavigate(node.path)} title={node.path || "root"}>
          <Folder size={14} />
          <span>{node.path ? node.name : "root"}</span>
        </button>
      </div>
      {isExpanded &&
        children.map((child) => (
          <TreeRow
            key={child.path}
            node={child}
            level={level + 1}
            current={current}
            expanded={expanded}
            onToggleExpanded={onToggleExpanded}
            onNavigate={onNavigate}
            onDropToFolder={onDropToFolder}
          />
        ))}
    </div>
  );
}

function SelectionBar({
  selectedCount,
  onCopy,
  onMove,
  onDelete
}: {
  selectedCount: number;
  onCopy: () => void;
  onMove: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="selection-actions">
      <span>{selectedCount ? `Выбрано: ${selectedCount}` : "Файлы"}</span>
      {selectedCount > 0 && (
        <div>
          <button onClick={onCopy}>
            <Copy size={14} /> <span className="action-label">Копировать</span>
          </button>
          <button onClick={onMove}>
            <MoveRight size={14} /> <span className="action-label">Переместить</span>
          </button>
          <button onClick={onDelete}>
            <Trash2 size={14} /> <span className="action-label">Удалить</span>
          </button>
        </div>
      )}
    </div>
  );
}

function FileList({
  cwd,
  entries,
  query,
  mode,
  selected,
  onNavigateUp,
  onOpen,
  onSelect,
  onContext,
  onDragPaths,
  onDropToFolder
}: {
  cwd: string;
  entries: FileEntry[];
  query: string;
  mode: ViewMode;
  selected: Set<string>;
  onNavigateUp: () => void;
  onOpen: (entry: FileEntry) => void | Promise<void>;
  onSelect: (entry: FileEntry, multi: boolean) => void;
  onContext: (event: MouseEvent, entry: FileEntry) => void;
  onDragPaths: (entry?: FileEntry) => string[];
  onDropToFolder: (event: DragEvent, targetDir: string) => void | Promise<void>;
}) {
  const emptyText = query.trim() ? "Ничего не найдено" : "Папка пуста";
  return (
    <div className={`file-list ${mode}`}>
      {mode === "details" && entries.length > 0 && (
        <div className="file-header-row">
          <span>Имя</span>
          <span>Тип</span>
          <span>Размер</span>
          <span>Изменен</span>
          <span />
        </div>
      )}
      {cwd && !query.trim() && (
        <button className="file-row up" onClick={onNavigateUp}>
          <ChevronsUp size={16} />
          <span>На уровень выше</span>
        </button>
      )}
      {entries.map((entry) => (
        <FileRow
          key={entry.path}
          entry={entry}
          selected={selected.has(entry.path)}
          onOpen={onOpen}
          onSelect={onSelect}
          onContext={onContext}
          onDragPaths={onDragPaths}
          onDropToFolder={onDropToFolder}
        />
      ))}
      {!entries.length && <div className="file-empty">{emptyText}</div>}
    </div>
  );
}

function FileRow({
  entry,
  selected,
  onOpen,
  onSelect,
  onContext,
  onDragPaths,
  onDropToFolder
}: {
  entry: FileEntry;
  selected: boolean;
  onOpen: (entry: FileEntry) => void | Promise<void>;
  onSelect: (entry: FileEntry, multi: boolean) => void;
  onContext: (event: MouseEvent, entry: FileEntry) => void;
  onDragPaths: (entry?: FileEntry) => string[];
  onDropToFolder: (event: DragEvent, targetDir: string) => void | Promise<void>;
}) {
  const compactMeta = `${entry.is_dir ? "Папка" : formatSize(entry.size_bytes)} · ${
    entry.modified_at
  }`;

  return (
    <div
      className={`file-row ${selected ? "selected" : ""}`}
      draggable
      onClick={(event) => onSelect(entry, event.metaKey || event.ctrlKey)}
      onDoubleClick={() => onOpen(entry)}
      onContextMenu={(event) => onContext(event, entry)}
      onDragStart={(event) => {
        event.dataTransfer.setData(
          "application/x-corpclaw-paths",
          JSON.stringify(onDragPaths(entry))
        );
      }}
      onDragOver={(event) => entry.is_dir && event.preventDefault()}
      onDrop={(event) => entry.is_dir && onDropToFolder(event, entry.path)}
      title={entry.path}
    >
      <span className="file-icon">{fileIcon(entry)}</span>
      <span className="file-name">
        <span className="file-name-main">{entry.name}</span>
        <span className="file-name-path">{entry.path || "root"}</span>
      </span>
      <span className="file-meta">{entry.is_dir ? "folder" : formatSize(entry.size_bytes)}</span>
      <span className="file-date">{entry.modified_at}</span>
      <span className="file-compact-meta">{compactMeta}</span>
      <button className="row-menu" onClick={(event) => onContext(event, entry)} title="Действия">
        <MoreVertical size={16} />
      </button>
    </div>
  );
}

function ContextMenu({
  context,
  onClose,
  onOpen,
  onPreview,
  onFullPreview,
  onRename,
  onCopy,
  onMove,
  onDelete
}: {
  context: { x: number; y: number; entry: FileEntry };
  onClose: () => void;
  onOpen: () => void;
  onPreview: () => void;
  onFullPreview: () => void;
  onRename: () => void;
  onCopy: () => void;
  onMove: () => void;
  onDelete: () => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ x: context.x, y: context.y });

  const fitToViewport = useCallback(() => {
    const menu = menuRef.current;
    if (!menu) return;

    const rect = menu.getBoundingClientRect();
    const margin = CONTEXT_MENU_VIEWPORT_MARGIN;
    const maxX = Math.max(margin, window.innerWidth - rect.width - margin);
    const maxY = Math.max(margin, window.innerHeight - rect.height - margin);

    setPosition({
      x: Math.min(Math.max(context.x, margin), maxX),
      y: Math.min(Math.max(context.y, margin), maxY)
    });
  }, [context.x, context.y]);

  useLayoutEffect(() => {
    setPosition({ x: context.x, y: context.y });
    fitToViewport();
  }, [context.entry.path, context.entry.is_dir, context.x, context.y, fitToViewport]);

  useEffect(() => {
    window.addEventListener("resize", fitToViewport);
    return () => window.removeEventListener("resize", fitToViewport);
  }, [fitToViewport]);

  return (
    <div ref={menuRef} className="context-menu" style={{ left: position.x, top: position.y }}>
      {context.entry.is_dir ? (
        <button
          onClick={() => {
            onOpen();
            onClose();
          }}
        >
          Открыть
        </button>
      ) : (
        <>
          <button
            onClick={() => {
              onFullPreview();
              onClose();
            }}
          >
            Просмотр в полном окне
          </button>
          <button
            onClick={() => {
              onPreview();
              onClose();
            }}
          >
            Предпросмотр
          </button>
        </>
      )}
      {!context.entry.is_dir && (
        <a href={downloadUrl(context.entry.path)} download={context.entry.name} onClick={onClose}>
          Скачать
        </a>
      )}
      <button
        onClick={() => {
          onRename();
          onClose();
        }}
      >
        Переименовать
      </button>
      <button
        onClick={() => {
          onCopy();
          onClose();
        }}
      >
        Копировать
      </button>
      <button
        onClick={() => {
          onMove();
          onClose();
        }}
      >
        Переместить
      </button>
      <button
        className="danger"
        onClick={() => {
          onDelete();
          onClose();
        }}
      >
        Удалить
      </button>
    </div>
  );
}

function UploadQueue({ uploads }: { uploads: UploadItem[] }) {
  if (!uploads.length) return null;
  return (
    <div className="upload-queue">
      {uploads.slice(-4).map((item) => (
        <div key={item.id} className={`upload-item ${item.status}`}>
          <span title={item.name}>{item.name}</span>
          <div>
            <i style={{ width: `${item.progress}%` }} />
          </div>
          {item.status === "error" && <small>{item.error}</small>}
        </div>
      ))}
    </div>
  );
}

function FileActionDialog({
  action,
  cwd,
  tree,
  onClose,
  onSubmit
}: {
  action: FileAction;
  cwd: string;
  tree: TreeNode | null;
  onClose: () => void;
  onSubmit: (action: FileAction, payload?: { name?: string; target?: string }) => Promise<void>;
}) {
  const [name, setName] = useState(action.type === "rename" ? action.entry.name : "");
  const [target, setTarget] = useState(cwd);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const folders = useMemo(() => flattenFolders(tree), [tree]);

  const title =
    action.type === "mkdir"
      ? "Новая папка"
      : action.type === "rename"
        ? "Переименовать"
        : action.type === "move"
          ? "Переместить"
          : action.type === "copy"
            ? "Копировать"
            : "Удалить";

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onSubmit(action, { name, target });
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "Ошибка операции");
    } finally {
      setBusy(false);
    }
  }

  if (action.type === "move" || action.type === "copy") {
    return (
      <Modal
        title={title}
        description={`Элементов: ${action.paths.length}`}
        onClose={onClose}
        footer={
          <>
            <button type="button" onClick={onClose}>
              Отмена
            </button>
            <button className="primary" type="button" disabled={busy} onClick={() => submit()}>
              {busy ? "Выполняю..." : title}
            </button>
          </>
        }
      >
        <div className="field">
          <label>Папка назначения</label>
          <div className="folder-picker">
            {folders.map((folder) => (
              <button
                type="button"
                key={folder.path}
                className={target === folder.path ? "active" : ""}
                onClick={() => setTarget(folder.path)}
              >
                <Folder size={14} />
                <span>{folder.path || "root"}</span>
              </button>
            ))}
          </div>
        </div>
        <PathList paths={action.paths} />
        {error && <div className="form-error">{error}</div>}
      </Modal>
    );
  }

  if (action.type === "delete") {
    return (
      <Modal
        title={title}
        description="Удаление нельзя отменить через интерфейс."
        onClose={onClose}
        footer={
          <>
            <button type="button" onClick={onClose}>
              Отмена
            </button>
            <button className="danger-button" type="button" disabled={busy} onClick={() => submit()}>
              {busy ? "Удаляю..." : "Удалить"}
            </button>
          </>
        }
      >
        <PathList paths={action.paths} />
        {error && <div className="form-error">{error}</div>}
      </Modal>
    );
  }

  return (
    <Modal
      title={title}
      onClose={onClose}
      onSubmit={() => submit()}
      footer={
        <>
          <button type="button" onClick={onClose}>
            Отмена
          </button>
          <button className="primary" disabled={busy}>
            {busy ? "Сохраняю..." : "Сохранить"}
          </button>
        </>
      }
    >
      <label className="field">
        <span>{action.type === "mkdir" ? "Имя папки" : "Новое имя"}</span>
        <input value={name} autoFocus onChange={(event) => setName(event.target.value)} />
      </label>
      {error && <div className="form-error">{error}</div>}
    </Modal>
  );
}

function PathList({ paths }: { paths: string[] }) {
  return (
    <div className="path-list">
      {paths.slice(0, 8).map((path) => (
        <div key={path} title={path}>
          {path}
        </div>
      ))}
      {paths.length > 8 && <small>И еще {paths.length - 8}</small>}
    </div>
  );
}
