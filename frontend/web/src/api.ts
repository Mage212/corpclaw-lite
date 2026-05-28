import type {
  DirectoryPayload,
  FileEntry,
  PreviewPayload,
  SessionPayload,
  TreeNode
} from "./types";

type ApiOptions = RequestInit & {
  csrf?: string;
};

async function parseJson<T>(response: Response): Promise<T> {
  const payload = (await response.json().catch(() => ({}))) as T & { error?: string };
  if (!response.ok) {
    throw new Error(payload.error || response.statusText || "Request failed");
  }
  return payload;
}

export function apiFetch<T>(path: string, options: ApiOptions = {}): Promise<T> {
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
  }).then(parseJson<T>);
}

export function getSession(): Promise<SessionPayload> {
  return apiFetch<SessionPayload>("/api/session");
}

export function login(username: string, password: string): Promise<SessionPayload> {
  return apiFetch<SessionPayload>("/api/login", {
    method: "POST",
    body: JSON.stringify({ username, password })
  });
}

export function logout(csrf: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>("/api/logout", {
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
  return apiFetch<DirectoryPayload>(`/api/files?${params.toString()}`);
}

export function searchFiles(query: string): Promise<{ query: string; entries: FileEntry[] }> {
  const params = new URLSearchParams({ query, limit: "200" });
  return apiFetch<{ query: string; entries: FileEntry[] }>(`/api/files/search?${params}`);
}

export function loadTree(): Promise<TreeNode> {
  return apiFetch<TreeNode>("/api/files/tree?depth=4");
}

export function previewFile(path: string): Promise<PreviewPayload> {
  return apiFetch<PreviewPayload>(`/api/files/preview?path=${encodeURIComponent(path)}`);
}

export function makeDirectory(csrf: string, path: string, name: string): Promise<{ path: string }> {
  return apiFetch<{ path: string }>("/api/files/mkdir", {
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
  return apiFetch<{ path: string }>("/api/files/rename", {
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
  return apiFetch<{ paths: string[] }>("/api/files/move", {
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
  return apiFetch<{ paths: string[] }>("/api/files/copy", {
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
  return apiFetch<{ paths: string[] }>("/api/files/delete", {
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
): Promise<{ uploaded: { name: string; path: string }[] }> {
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
        const payload = JSON.parse(xhr.responseText || "{}") as {
          uploaded?: { name: string; path: string }[];
          error?: string;
        };
        if (xhr.status >= 200 && xhr.status < 300 && payload.uploaded) {
          resolve({ uploaded: payload.uploaded });
          return;
        }
        reject(new Error(payload.error || xhr.statusText || "Upload failed"));
      } catch (error) {
        reject(error instanceof Error ? error : new Error("Upload failed"));
      }
    };
    xhr.onerror = () => reject(new Error("Upload failed"));
    xhr.send(form);
  });
}
