import { Archive, File, FileImage, FileSpreadsheet, Folder } from "lucide-react";
import type { FileEntry, TreeNode } from "../types";

export function fileIcon(entry: FileEntry, size = 18) {
  if (entry.is_dir) return <Folder size={size} />;
  if (entry.kind === "image") return <FileImage size={size} />;
  if (entry.kind === "spreadsheet") return <FileSpreadsheet size={size} />;
  if (entry.kind === "archive") return <Archive size={size} />;
  return <File size={size} />;
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function parentPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

export function pathAncestors(path: string): string[] {
  const parts = path.split("/").filter(Boolean);
  return parts.map((_part, index) => parts.slice(0, index + 1).join("/"));
}

export function flattenFolders(node: TreeNode | null): TreeNode[] {
  if (!node) return [];
  const folders: TreeNode[] = [node];
  for (const child of node.children || []) {
    folders.push(...flattenFolders(child));
  }
  return folders.filter((item) => item.is_dir);
}
