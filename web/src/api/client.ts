import type { AssetListResponse, SortMode, VolumeId, VolumeInfo } from "../types";

async function parseError(response: Response): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: string };
    return data.detail || response.statusText;
  } catch {
    return response.statusText || "Request failed";
  }
}

export async function fetchVolumes(signal?: AbortSignal): Promise<VolumeInfo[]> {
  const response = await fetch("/api/volumes", { signal });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function fetchAssets(
  params: {
    volume: VolumeId | string;
    path?: string;
    search?: string;
    sort?: SortMode;
    page?: number;
    pageSize?: number;
    /** Bypass server list cache (use after upload/move/delete). */
    refresh?: boolean;
  },
  signal?: AbortSignal,
): Promise<AssetListResponse> {
  const query = new URLSearchParams();
  query.set("volume", params.volume);
  if (params.path) query.set("path", params.path);
  if (params.search) query.set("search", params.search);
  if (params.sort) query.set("sort", params.sort);
  if (params.page) query.set("page", String(params.page));
  if (params.pageSize) query.set("page_size", String(params.pageSize));
  if (params.refresh) query.set("refresh", "1");

  const response = await fetch(`/api/assets?${query}`, {
    signal,
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export function thumbnailUrl(
  volume: string,
  path: string,
  meta?: {
    name?: string;
    size?: number;
    modified_at?: string;
    media_type?: string;
  },
): string {
  const query = new URLSearchParams({ volume, path, kind: "file" });
  if (meta?.name) query.set("name", meta.name);
  if (meta?.size != null) query.set("size", String(meta.size));
  if (meta?.modified_at) query.set("modified_at", meta.modified_at);
  if (meta?.media_type) query.set("media_type", meta.media_type);
  return `/api/assets/thumbnail?${query}`;
}

export function contentUrl(
  volume: string,
  path: string,
  download = false,
  meta?: {
    name?: string;
    kind?: string;
    size?: number;
    modified_at?: string;
    media_type?: string;
  },
): string {
  const query = new URLSearchParams({ volume, path });
  if (download) query.set("download", "true");
  if (meta?.name) query.set("name", meta.name);
  if (meta?.kind) query.set("kind", meta.kind);
  if (meta?.size != null) query.set("size", String(meta.size));
  if (meta?.modified_at) query.set("modified_at", meta.modified_at);
  if (meta?.media_type) query.set("media_type", meta.media_type);
  return `/api/assets/content?${query}`;
}

export async function uploadFiles(params: {
  volume: string;
  destination: string;
  overwrite: boolean;
  files: File[];
}): Promise<{ message: string; paths: string[] }> {
  const body = new FormData();
  body.set("volume", params.volume);
  body.set("destination", params.destination);
  body.set("overwrite", params.overwrite ? "true" : "false");
  for (const file of params.files) {
    body.append("files", file);
  }
  const response = await fetch("/api/assets/upload", { method: "POST", body });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function moveAsset(params: {
  source_volume: string;
  source_path: string;
  destination_volume: string;
  destination_path: string;
  overwrite: boolean;
}): Promise<{ message: string; paths: string[] }> {
  const response = await fetch("/api/assets/move", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function deleteAsset(params: {
  volume: string;
  path: string;
  recursive: boolean;
}): Promise<{
  message: string;
  paths: string[];
  failed?: { path: string; error: string }[];
}> {
  const response = await fetch("/api/assets", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export type DeleteProgress = {
  done: number;
  failed: number;
  total: number;
  processed: number;
};

export async function deleteAssets(params: {
  volume: string;
  items: { path: string; recursive?: boolean }[];
  workers?: number;
  onProgress?: (progress: DeleteProgress) => void;
}): Promise<{
  message: string;
  paths: string[];
  failed: { path: string; error: string }[];
  done?: number;
  failed_count?: number;
  total?: number;
}> {
  const response = await fetch("/api/assets", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      volume: params.volume,
      items: params.items,
      workers: params.workers ?? 4,
      stream: true,
    }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  if (!response.body) throw new Error("Empty delete response body");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: {
    message: string;
    paths: string[];
    failed: { path: string; error: string }[];
    done?: number;
    failed_count?: number;
    total?: number;
  } | null = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const event = JSON.parse(trimmed) as {
        type: string;
        done?: number;
        failed?: number | { path: string; error: string }[];
        total?: number;
        processed?: number;
        detail?: string;
        message?: string;
        paths?: string[];
        failed_count?: number;
      };
      if (event.type === "progress") {
        params.onProgress?.({
          done: event.done ?? 0,
          failed: typeof event.failed === "number" ? event.failed : 0,
          total: event.total ?? params.items.length,
          processed: event.processed ?? 0,
        });
      } else if (event.type === "done") {
        finalResult = {
          message: event.message ?? "削除完了",
          paths: event.paths ?? [],
          failed: Array.isArray(event.failed) ? event.failed : [],
          done: event.done,
          failed_count: event.failed_count,
          total: event.total,
        };
      } else if (event.type === "error") {
        throw new Error(event.detail || "Delete failed");
      }
    }
  }

  if (!finalResult) {
    throw new Error("Delete stream ended without a result");
  }
  return finalResult;
}
