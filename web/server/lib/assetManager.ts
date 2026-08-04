import { defaultPythonBridge, type PythonAssetBridge } from "./pythonBridge.js";
import {
  type AssetEntry,
  type AssetListResponse,
  type MaterializedFile,
  type SortMode,
} from "./types.js";
import { normalizeVolumePath, validateVolume } from "./paths.js";

export type AssetManagerOptions = {
  bridge?: PythonAssetBridge;
};

/**
 * Thin client over the warm Python Modal SDK worker.
 * Listing/pagination/sort happen in Python (small page payloads only).
 */
export class AssetManager {
  private readonly bridge: PythonAssetBridge;
  private readonly materializeInflight = new Map<string, Promise<MaterializedFile>>();
  private mutationTail: Promise<void> = Promise.resolve();

  constructor(options: AssetManagerOptions = {}) {
    this.bridge = options.bridge ?? defaultPythonBridge;
  }

  private runWithLock<T>(fn: () => Promise<T>): Promise<T> {
    const run = this.mutationTail.then(fn, fn);
    this.mutationTail = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  async listAssets(
    volume: string,
    dirPath = "",
    options: {
      search?: string;
      sort?: SortMode;
      page?: number;
      pageSize?: number;
      refresh?: boolean;
    } = {},
  ): Promise<AssetListResponse> {
    validateVolume(volume);
    const path = normalizeVolumePath(dirPath, { allowRoot: true });
    return this.bridge.call<AssetListResponse>("list", {
      volume,
      path,
      search: options.search ?? "",
      sort: options.sort ?? "name_asc",
      page: options.page ?? 1,
      page_size: options.pageSize ?? 100,
      refresh: Boolean(options.refresh),
    });
  }

  async materialize(
    volume: string,
    remotePath: string,
    options: {
      imageOnly?: boolean;
      entry?: AssetEntry | null;
    } = {},
  ): Promise<MaterializedFile> {
    validateVolume(volume);
    const normalized = normalizeVolumePath(remotePath, { allowRoot: false });
    const key = `${volume}:${normalized}:${options.imageOnly ? "img" : "bin"}`;
    const existing = this.materializeInflight.get(key);
    if (existing) return existing;

    const params: Record<string, unknown> = {
      volume,
      path: normalized,
      image_only: Boolean(options.imageOnly),
    };
    const entry = options.entry;
    if (entry) {
      params.name = entry.name;
      params.kind = entry.kind;
      params.size = entry.size;
      params.modified_at = entry.modified_at;
      params.media_type = entry.media_type;
    }

    const request = this.bridge
      .call<{
        path: string;
        name: string;
        media_type: string;
        size: number;
      }>("materialize", params)
      .then((result) => ({
        localPath: result.path,
        name: result.name,
        mediaType: result.media_type,
        size: result.size,
      }))
      .finally(() => {
        this.materializeInflight.delete(key);
      });
    this.materializeInflight.set(key, request);
    return request;
  }

  async upload(
    volume: string,
    destination: string,
    localFiles: string[],
    overwrite = false,
  ): Promise<{ message: string; paths: string[] }> {
    return this.runWithLock(() =>
      this.bridge.call("upload", {
        volume,
        destination,
        files: localFiles,
        overwrite,
      }),
    );
  }

  async move(params: {
    sourceVolume: string;
    sourcePath: string;
    destinationVolume: string;
    destinationPath: string;
    overwrite?: boolean;
  }): Promise<{ message: string; paths: string[] }> {
    return this.runWithLock(() =>
      this.bridge.call("move", {
        source_volume: params.sourceVolume,
        source_path: params.sourcePath,
        destination_volume: params.destinationVolume,
        destination_path: params.destinationPath,
        overwrite: Boolean(params.overwrite),
      }),
    );
  }

  async delete(
    volume: string,
    remotePath: string,
    recursive = false,
  ): Promise<{ message: string; paths: string[]; failed: { path: string; error: string }[] }> {
    return this.runWithLock(() =>
      this.bridge.call("delete", {
        volume,
        path: remotePath,
        recursive,
      }),
    );
  }

  async deleteMany(
    volume: string,
    items: { path: string; recursive?: boolean }[],
    options: {
      workers?: number;
      onProgress?: (progress: {
        done: number;
        failed: number;
        total: number;
        processed: number;
      }) => void;
    } = {},
  ): Promise<{
    message: string;
    paths: string[];
    failed: { path: string; error: string }[];
    done?: number;
    failed_count?: number;
    total?: number;
  }> {
    // Do not hold mutationTail across the whole batch — parallelism lives in Python.
    return this.bridge.call(
      "delete_many",
      {
        volume,
        items,
        workers: options.workers ?? 4,
      },
      {
        onProgress: (value) => {
          const progress = value as {
            done: number;
            failed: number;
            total: number;
            processed: number;
          };
          options.onProgress?.(progress);
        },
      },
    );
  }
}

export function sortEntries(entries: AssetEntry[], sortMode: SortMode): AssetEntry[] {
  const reverse = sortMode.endsWith("_desc");
  const sorted = [...entries].sort((a, b) => {
    let cmp = 0;
    if (sortMode.startsWith("modified")) {
      cmp = a.modified_at.localeCompare(b.modified_at);
    } else if (sortMode.startsWith("size")) {
      cmp = a.size - b.size;
    } else {
      cmp = a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    }
    return reverse ? -cmp : cmp;
  });
  return sorted.sort((a, b) => Number(b.is_directory) - Number(a.is_directory));
}

export function parseHumanSize(raw: string): number {
  const text = raw.trim();
  if (!text) return 0;
  const match = text.match(/^([\d.]+)\s*([KMGT]?i?B)$/i);
  if (!match) {
    const asInt = Number.parseInt(text, 10);
    return Number.isFinite(asInt) ? asInt : 0;
  }
  const value = Number.parseFloat(match[1]);
  const unit = match[2].toUpperCase();
  const table: Record<string, number> = {
    B: 1,
    KB: 1000,
    MB: 1000 ** 2,
    GB: 1000 ** 3,
    TB: 1000 ** 4,
    KIB: 1024,
    MIB: 1024 ** 2,
    GIB: 1024 ** 3,
    TIB: 1024 ** 4,
  };
  return Math.round(value * (table[unit] ?? 1));
}

export const defaultAssetManager = new AssetManager();
