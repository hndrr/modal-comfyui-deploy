import path from "node:path";
import {
  ALLOWED_VOLUMES,
  COMFY_MODEL_SUBDIRS,
  IMAGE_EXTENSIONS,
  VIDEO_EXTENSIONS,
  AUDIO_EXTENSIONS,
  type AssetKind,
} from "./types.js";

export function normalizeVolumePath(
  rawPath: string,
  options: { allowRoot?: boolean } = {},
): string {
  const allowRoot = options.allowRoot ?? true;
  const raw = String(rawPath ?? "").trim();
  if (raw.includes("\\")) {
    throw new Error("Volume paths must use forward slashes.");
  }
  if (raw.includes("\0")) {
    throw new Error("Volume paths cannot contain NUL bytes.");
  }
  if (raw === "" || raw === ".") {
    if (allowRoot) return "";
    throw new Error("The Volume root cannot be used for this operation.");
  }
  if (raw.startsWith("/")) {
    throw new Error("Volume paths must be relative.");
  }
  const parts = raw.split("/").filter((part) => part && part !== ".");
  if (parts.some((part) => part === "..")) {
    throw new Error("Volume paths cannot contain '..'.");
  }
  const normalized = parts.join("/");
  if (!normalized) {
    if (allowRoot) return "";
    throw new Error("The Volume root cannot be used for this operation.");
  }
  return normalized;
}

export function validateVolume(volume: string): string {
  if (!ALLOWED_VOLUMES.has(volume)) {
    const allowed = [...ALLOWED_VOLUMES].sort().join(", ");
    throw new Error(`Unsupported Volume ${JSON.stringify(volume)}. Allowed Volumes: ${allowed}`);
  }
  return volume;
}

export function validateModelDestination(destPath: string): void {
  const normalized = normalizeVolumePath(destPath, { allowRoot: false });
  const top = normalized.split("/")[0];
  if (!COMFY_MODEL_SUBDIRS.has(top)) {
    const allowed = [...COMFY_MODEL_SUBDIRS].sort().join(", ");
    throw new Error(`Model destinations must be under one of: ${allowed}`);
  }
}

export function classifyMedia(filePath: string, kind: AssetKind): string {
  if (kind === "directory") return "directory";
  const suffix = path.posix.extname(filePath).toLowerCase();
  if (IMAGE_EXTENSIONS.has(suffix)) return "image";
  if (VIDEO_EXTENSIONS.has(suffix)) return "video";
  if (AUDIO_EXTENSIONS.has(suffix)) return "audio";
  return "file";
}

export function joinVolumePath(dir: string, name: string): string {
  const base = normalizeVolumePath(dir, { allowRoot: true });
  // Listing entries are a single segment. Allow unusual characters (incl. `\`)
  // that appear in real filenames; only block path separators and empties.
  const leaf = String(name ?? "").replace(/^\/+/, "").replace(/\/+$/, "");
  if (!leaf || leaf === "." || leaf === "..") {
    throw new Error("Invalid file name.");
  }
  if (leaf.includes("/") || leaf.includes("\0")) {
    throw new Error("Invalid file name.");
  }
  return base ? `${base}/${leaf}` : leaf;
}

export function parentPath(volumePath: string): string {
  const normalized = normalizeVolumePath(volumePath, { allowRoot: true });
  if (!normalized) return "";
  const parts = normalized.split("/");
  parts.pop();
  return parts.join("/");
}

export function breadcrumb(pathValue: string): { label: string; path: string }[] {
  const items = [{ label: "/", path: "" }];
  const parts: string[] = [];
  const normalized = normalizeVolumePath(pathValue, { allowRoot: true });
  for (const part of normalized ? normalized.split("/") : []) {
    parts.push(part);
    items.push({ label: part, path: parts.join("/") });
  }
  return items;
}
