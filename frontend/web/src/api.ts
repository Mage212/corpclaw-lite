import type {
  DirectoryPayload,
  FileEntry,
  PreviewPayload,
  SessionPayload,
  TreeNode
} from "./types";
import {
  errorMessageFromPayload,
  parseDirectoryPayload,
  parseOkPayload,
  parsePathPayload,
  parsePathsPayload,
  parsePreviewPayload,
  parseSearchPayload,
  parseSessionPayload,
  parseTreeNode,
  parseUploadPayload
} from "./contracts";
import type { UploadPayload } from "./contracts";

type ApiOptions = RequestInit & {
  csrf?: string;
};

async function readJson(response: Response): Promise<unknown> {
  return response.json().catch(() => ({}));
}

async function parseJson<T>(response: Response, parser: (value: unknown) => T): Promise<T> {
  const payload = await readJson(response);
  if (!response.ok) {
    throw new Error(errorMessageFromPayload(payload) || response.statusText || "Request failed");
  }
  return parser(payload);
}

export function apiFetch<T>(
  path: string,
  parser: (value: unknown) => T,
  options: ApiOptions = {}
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.csrf) {
    headers.set("X-CSRF-Token", options.csrf);
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(path, {
    ...options,
    headers
  }).then((response) => parseJson(response, parser));
}

export function getSession(): Promise<SessionPayload> {
  return apiFetch("/api/session", parseSessionPayload);
}

export function login(username: string, password: string): Promise<SessionPayload> {
  return apiFetch("/api/login", parseSessionPayload, {
    method: "POST",
    body: JSON.stringify({ username, password })
  });
}

export function logout(csrf: string): Promise<{ ok: boolean }> {
  return apiFetch("/api/logout", parseOkPayload, {
    method: "POST",
    csrf
  });
}

export function listFiles(
  path: string,
  sort = "name",
  order = "asc"
): Promise<DirectoryPayload> {
  const params = new URLSearchParams({ path, sort, order });
  return apiFetch(`/api/files?${params.toString()}`, parseDirectoryPayload);
}

export function searchFiles(query: string): Promise<{ query: string; entries: FileEntry[] }> {
  const params = new URLSearchParams({ query, limit: "200" });
  return apiFetch(`/api/files/search?${params}`, parseSearchPayload);
}

export function loadTree(): Promise<TreeNode> {
  return apiFetch("/api/files/tree?depth=4", parseTreeNode);
}

export function previewFile(path: string): Promise<PreviewPayload> {
  return apiFetch(`/api/files/preview?path=${encodeURIComponent(path)}`, parsePreviewPayload);
}

export function makeDirectory(csrf: string, path: string, name: string): Promise<{ path: string }> {
  return apiFetch("/api/files/mkdir", parsePathPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ path, name })
  });
}

export function renameFile(
  csrf: string,
  path: string,
  newName: string
): Promise<{ path: string }> {
  return apiFetch("/api/files/rename", parsePathPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ path, new_name: newName })
  });
}

export function moveFiles(
  csrf: string,
  paths: string[],
  targetDir: string
): Promise<{ paths: string[] }> {
  return apiFetch("/api/files/move", parsePathsPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ paths, target_dir: targetDir })
  });
}

export function copyFiles(
  csrf: string,
  paths: string[],
  targetDir: string
): Promise<{ paths: string[] }> {
  return apiFetch("/api/files/copy", parsePathsPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ paths, target_dir: targetDir })
  });
}

export function deleteFiles(
  csrf: string,
  paths: string[],
  recursive: boolean
): Promise<{ paths: string[] }> {
  return apiFetch("/api/files/delete", parsePathsPayload, {
    method: "POST",
    csrf,
    body: JSON.stringify({ paths, recursive })
  });
}

export function downloadUrl(path: string): string {
  return `/api/files/download?path=${encodeURIComponent(path)}`;
}

export function uploadFiles(
  csrf: string,
  path: string,
  files: File[],
  onProgress: (fileName: string, progress: number) => void
): Promise<UploadPayload> {
  const form = new FormData();
  for (const file of files) {
    form.append("file", file);
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/files/upload?path=${encodeURIComponent(path)}`);
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) {
        return;
      }
      const progress = Math.round((event.loaded / event.total) * 100);
      for (const file of files) {
        onProgress(file.name, progress);
      }
    };
    xhr.onload = () => {
      try {
        const payload: unknown = JSON.parse(xhr.responseText || "{}");
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(parseUploadPayload(payload));
          return;
        }
        reject(new Error(errorMessageFromPayload(payload) || xhr.statusText || "Upload failed"));
      } catch (error) {
        reject(error instanceof Error ? error : new Error("Upload failed"));
      }
    };
    xhr.onerror = () => reject(new Error("Upload failed"));
    xhr.send(form);
  });
}
