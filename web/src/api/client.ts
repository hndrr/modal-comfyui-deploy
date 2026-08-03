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

  const response = await fetch(`/api/assets?${query}`, { signal });
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
}): Promise<{ message: string; paths: string[] }> {
  const response = await fetch("/api/assets", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}
