import Busboy from "busboy";
import { Hono } from "hono";
import { cors } from "hono/cors";
import { HTTPException } from "hono/http-exception";
import type { ContentfulStatusCode } from "hono/utils/http-status";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import { fileURLToPath } from "node:url";
import { AssetManager, defaultAssetManager } from "./lib/assetManager.js";
import {
  INPUT_VOLUME,
  MODEL_VOLUME,
  OUTPUT_VOLUME,
  SORT_CHOICES,
  VOLUME_LABELS,
  type MaterializedFile,
  type SortMode,
} from "./lib/types.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(__dirname, "..");
const DIST_DIR = path.join(WEB_ROOT, "dist");

export type AppDeps = {
  manager?: AssetManager;
  maxUploadFileBytes?: number;
  maxUploadTotalBytes?: number;
};

const DEFAULT_MAX_UPLOAD_FILE_BYTES = 10 * 1024 * 1024 * 1024;
const DEFAULT_MAX_UPLOAD_TOTAL_BYTES = 20 * 1024 * 1024 * 1024;
const UPLOAD_FIELDS = new Set(["volume", "destination", "overwrite"]);

function envByteLimit(name: string, fallback: number): number {
  const parsed = Number(process.env[name]);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function requirePositiveByteLimit(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive safe integer.`);
  }
  return value;
}

function httpError(error: unknown, status: ContentfulStatusCode = 400): HTTPException {
  if (error instanceof HTTPException) return error;
  const message = error instanceof Error ? error.message : String(error);
  return new HTTPException(status, { message });
}

function payloadTooLarge(message: string): HTTPException {
  return new HTTPException(413, { message });
}

export function createAssetEtag(source: string): string {
  return `"${createHash("sha256").update(source).digest("base64url")}"`;
}

function encodeRfc5987Value(value: string): string {
  return encodeURIComponent(value).replace(/[!'()*]/g, (character) =>
    `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

export function attachmentContentDisposition(fileName: string): string {
  const fallback =
    fileName.replace(/[^\x20-\x7E]/g, "_").replace(/["\\]/g, "_") ||
    "download";
  return `attachment; filename="${fallback}"; filename*=UTF-8''${encodeRfc5987Value(fileName)}`;
}

async function parseAndStageUpload(
  request: Request,
  tmpDir: string,
  maxFileBytes: number,
  maxTotalBytes: number,
): Promise<{
  volume: string;
  destination: string;
  overwrite: boolean;
  localPaths: string[];
}> {
  if (!request.body) throw new Error("Upload request body is required.");

  const contentLength = Number(request.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > maxTotalBytes) {
    throw payloadTooLarge(
      `Upload request exceeds the ${maxTotalBytes}-byte limit.`,
    );
  }

  const parser = Busboy({
    headers: Object.fromEntries(request.headers.entries()),
    defParamCharset: "utf8",
    limits: {
      fieldSize: 4096,
      fields: 3,
      fileSize: maxFileBytes,
      files: 100,
      parts: 103,
    },
  });
  const fields = new Map<string, string>();
  const stagedNames = new Set<string>();
  const localPaths: string[] = [];
  const writes: Promise<void>[] = [];
  let parseError: Error | null = null;

  const recordError = (error: unknown) => {
    if (!parseError) {
      parseError = error instanceof Error ? error : new Error(String(error));
    }
  };

  parser.on("field", (name, value, info) => {
    if (info.valueTruncated) {
      recordError(new Error(`Upload field is too large: ${name}`));
      return;
    }
    if (!UPLOAD_FIELDS.has(name)) return;
    if (fields.has(name)) {
      recordError(new Error(`Duplicate upload field: ${name}`));
      return;
    }
    fields.set(name, value);
  });

  parser.on("file", (fieldName, stream, info) => {
    if (fieldName !== "files") {
      recordError(new Error(`Unexpected upload file field: ${fieldName}`));
      stream.resume();
      return;
    }

    const safeName = path.basename(info.filename || "upload.bin");
    if (!safeName || safeName === "." || safeName === "..") {
      recordError(new Error("Invalid upload filename."));
      stream.resume();
      return;
    }
    if (stagedNames.has(safeName)) {
      recordError(
        new Error(
          `Duplicate upload filename in one request: ${safeName}. Rename or upload separately.`,
        ),
      );
      stream.resume();
      return;
    }
    stagedNames.add(safeName);

    const stageDir = fs.mkdtempSync(path.join(tmpDir, "f-"));
    const localPath = path.join(stageDir, safeName);
    stream.once("limit", () => {
      recordError(
        payloadTooLarge(
          `Upload file ${safeName} exceeds the ${maxFileBytes}-byte limit.`,
        ),
      );
    });
    writes.push(
      pipeline(stream, fs.createWriteStream(localPath, { flags: "wx" })).catch(
        recordError,
      ),
    );
    localPaths.push(localPath);
  });

  parser.once("filesLimit", () =>
    recordError(payloadTooLarge("Upload contains too many files.")),
  );
  parser.once("fieldsLimit", () =>
    recordError(new Error("Upload contains too many fields.")),
  );
  parser.once("partsLimit", () =>
    recordError(payloadTooLarge("Upload contains too many multipart sections.")),
  );

  let received = 0;
  const requestLimiter = new Transform({
    transform(chunk: Buffer, _encoding, callback) {
      received += chunk.length;
      if (received > maxTotalBytes) {
        callback(
          payloadTooLarge(
            `Upload request exceeds the ${maxTotalBytes}-byte limit.`,
          ),
        );
        return;
      }
      callback(null, chunk);
    },
  });

  try {
    await pipeline(
      Readable.from(request.body as AsyncIterable<Uint8Array>),
      requestLimiter,
      parser,
    );
  } catch (error) {
    recordError(error);
  }
  await Promise.all(writes);
  if (parseError) throw parseError;
  if (!localPaths.length) throw new Error("Select at least one file to upload.");

  const overwriteRaw = fields.get("overwrite") ?? "";
  return {
    volume: fields.get("volume") ?? "",
    destination: fields.get("destination") ?? "",
    overwrite:
      overwriteRaw === "true" ||
      overwriteRaw === "1" ||
      overwriteRaw.toLowerCase() === "on",
    localPaths,
  };
}

type ByteRange = { start: number; end: number };

function parseByteRange(
  value: string | undefined,
  size: number,
): ByteRange | null | "invalid" {
  if (!value) return null;
  const match = /^bytes=(\d*)-(\d*)$/i.exec(value.trim());
  if (!match || (!match[1] && !match[2]) || size <= 0) return "invalid";

  if (!match[1]) {
    const suffixLength = Number(match[2]);
    if (!Number.isSafeInteger(suffixLength) || suffixLength <= 0) {
      return "invalid";
    }
    return { start: Math.max(0, size - suffixLength), end: size - 1 };
  }

  const start = Number(match[1]);
  const requestedEnd = match[2] ? Number(match[2]) : size - 1;
  if (
    !Number.isSafeInteger(start) ||
    !Number.isSafeInteger(requestedEnd) ||
    start < 0 ||
    start >= size ||
    requestedEnd < start
  ) {
    return "invalid";
  }
  return { start, end: Math.min(requestedEnd, size - 1) };
}

async function removeStreamLease(localPath: string): Promise<void> {
  try {
    await fs.promises.rm(localPath, { force: true });
  } catch (error) {
    console.error(`[asset_stream] failed to remove ${localPath}:`, error);
  }
}

async function cleanupMaterializedFile(file: MaterializedFile): Promise<void> {
  if (file.cleanupAfterStream) await removeStreamLease(file.localPath);
}

/** Stream a local file without buffering it, with single-range seek support. */
async function streamFileResponse(
  localPath: string,
  headers: Record<string, string>,
  options: { range?: string; cleanup?: boolean } = {},
): Promise<Response> {
  let stat: fs.Stats;
  try {
    stat = await fs.promises.stat(localPath);
    if (!stat.isFile()) throw new Error(`Not a file: ${localPath}`);
  } catch (error) {
    if (options.cleanup) await removeStreamLease(localPath);
    throw error;
  }

  const range = parseByteRange(options.range, stat.size);
  if (range === "invalid") {
    if (options.cleanup) await removeStreamLease(localPath);
    return new Response(null, {
      status: 416,
      headers: {
        ...headers,
        "Accept-Ranges": "bytes",
        "Content-Range": `bytes */${stat.size}`,
        "Content-Length": "0",
      },
    });
  }

  const start = range?.start ?? 0;
  const end = range?.end ?? Math.max(0, stat.size - 1);
  const nodeStream = fs.createReadStream(
    localPath,
    range ? { start, end } : undefined,
  );
  let cleanupStarted = false;
  const cleanup = () => {
    if (!options.cleanup || cleanupStarted) return;
    cleanupStarted = true;
    void removeStreamLease(localPath);
  };
  nodeStream.once("error", (error) => {
    console.error(`[asset_stream] failed to stream ${localPath}:`, error);
    cleanup();
  });
  nodeStream.once("close", cleanup);
  const webStream = Readable.toWeb(nodeStream) as ReadableStream<Uint8Array>;
  return new Response(webStream, {
    status: range ? 206 : 200,
    headers: {
      ...headers,
      "Accept-Ranges": "bytes",
      "Content-Length": String(range ? end - start + 1 : stat.size),
      ...(range ? { "Content-Range": `bytes ${start}-${end}/${stat.size}` } : {}),
    },
  });
}

export function createApp(deps: AppDeps = {}) {
  const manager = deps.manager ?? defaultAssetManager;
  const maxUploadFileBytes = requirePositiveByteLimit(
    deps.maxUploadFileBytes ??
      envByteLimit("ASSET_UPLOAD_MAX_FILE_BYTES", DEFAULT_MAX_UPLOAD_FILE_BYTES),
    "maxUploadFileBytes",
  );
  const maxUploadTotalBytes = requirePositiveByteLimit(
    deps.maxUploadTotalBytes ??
      envByteLimit("ASSET_UPLOAD_MAX_TOTAL_BYTES", DEFAULT_MAX_UPLOAD_TOTAL_BYTES),
    "maxUploadTotalBytes",
  );
  const app = new Hono();

  app.use(
    "/api/*",
    cors({
      origin: ["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:7860"],
    }),
  );

  app.onError((error, c) => {
    if (error instanceof HTTPException) {
      return c.json({ detail: error.message }, error.status);
    }
    console.error(error);
    return c.json(
      { detail: error instanceof Error ? error.message : "Internal Server Error" },
      500,
    );
  });

  app.get("/api/health", (c) => c.json({ status: "ok" }));

  app.get("/api/volumes", (c) =>
    c.json(
      [INPUT_VOLUME, OUTPUT_VOLUME, MODEL_VOLUME].map((id) => ({
        id,
        label: VOLUME_LABELS[id],
      })),
    ),
  );

  app.get("/api/assets", async (c) => {
    try {
      const volume = c.req.query("volume");
      if (!volume) throw new Error("volume is required");
      const sort = (c.req.query("sort") ?? "modified_desc") as SortMode;
      if (!SORT_CHOICES.includes(sort)) {
        throw new Error(`Unsupported sort mode: ${sort}`);
      }
      const page = Number(c.req.query("page") ?? "1");
      const pageSize = Number(c.req.query("page_size") ?? "100");
      const refresh =
        c.req.query("refresh") === "1" ||
        c.req.query("refresh") === "true" ||
        c.req.query("refresh") === "yes";
      const result = await manager.listAssets(volume, c.req.query("path") ?? "", {
        search: c.req.query("search") ?? "",
        sort,
        page: Number.isFinite(page) ? page : 1,
        pageSize: Number.isFinite(pageSize) ? pageSize : 100,
        refresh,
      });
      return c.json(result);
    } catch (error) {
      throw httpError(error, 400);
    }
  });

  app.get("/api/assets/content", async (c) => {
    try {
      const volume = c.req.query("volume");
      const remotePath = c.req.query("path");
      if (!volume || !remotePath) throw new Error("volume and path are required");
      const download = c.req.query("download") === "true" || c.req.query("download") === "1";
      // Optional metadata from list response skips a re-listdir on the Python side.
      const entryMeta = {
        name: c.req.query("name") ?? undefined,
        kind: c.req.query("kind") ?? undefined,
        size: c.req.query("size") ? Number(c.req.query("size")) : undefined,
        modified_at: c.req.query("modified_at") ?? undefined,
        media_type: c.req.query("media_type") ?? undefined,
      };
      const hasMeta = Boolean(entryMeta.modified_at && entryMeta.kind);
      const file = await manager.materialize(volume, remotePath, {
        entry: hasMeta
          ? {
              volume,
              path: remotePath,
              name: entryMeta.name || remotePath.split("/").pop() || remotePath,
              kind: (entryMeta.kind as "file" | "directory" | "symlink") || "file",
              size: entryMeta.size || 0,
              modified_at: entryMeta.modified_at || new Date(0).toISOString(),
              media_type: entryMeta.media_type || "file",
              is_directory: entryMeta.kind === "directory",
            }
          : null,
      });
      return await streamFileResponse(
        file.localPath,
        {
          "Content-Type": file.mediaType,
          ...(download
            ? {
                "Content-Disposition": attachmentContentDisposition(file.name),
              }
            : {}),
        },
        {
          range: c.req.header("range"),
          cleanup: file.cleanupAfterStream,
        },
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const status: ContentfulStatusCode = message.includes("does not exist") ? 404 : 400;
      throw httpError(error, status);
    }
  });

  app.get("/api/assets/thumbnail", async (c) => {
    try {
      const volume = c.req.query("volume");
      const remotePath = c.req.query("path");
      if (!volume || !remotePath) throw new Error("volume and path are required");
      const mediaType = c.req.query("media_type") ?? "image";
      if (mediaType !== "image" && mediaType !== "video") {
        throw new Error("Thumbnails are only available for images and videos.");
      }
      const entryMeta = {
        name: c.req.query("name") ?? undefined,
        kind: c.req.query("kind") ?? "file",
        size: c.req.query("size") ? Number(c.req.query("size")) : 0,
        modified_at: c.req.query("modified_at") ?? undefined,
        media_type: mediaType,
      };
      const hasMeta = Boolean(entryMeta.modified_at);
      const file = await manager.materialize(volume, remotePath, {
        imageOnly: true,
        entry: hasMeta
          ? {
              volume,
              path: remotePath,
              name: entryMeta.name || remotePath.split("/").pop() || remotePath,
              kind: "file",
              size: entryMeta.size || 0,
              modified_at: entryMeta.modified_at || new Date(0).toISOString(),
              media_type: mediaType,
              is_directory: false,
            }
          : null,
      });
      // ETag from durable cache key (volume+path+mtime+size+thumb size).
      const etagSource = `${volume}:${remotePath}:${entryMeta.modified_at ?? ""}:${entryMeta.size}:${file.size}`;
      const etag = createAssetEtag(etagSource);
      const inm = c.req.header("if-none-match");
      if (inm && inm === etag) {
        await cleanupMaterializedFile(file);
        return new Response(null, {
          status: 304,
          headers: {
            ETag: etag,
            "Cache-Control": "private, max-age=604800, immutable",
          },
        });
      }
      return await streamFileResponse(
        file.localPath,
        {
          "Content-Type": file.mediaType || "image/jpeg",
          "Cache-Control": "private, max-age=604800, immutable",
          ETag: etag,
        },
        {
          range: c.req.header("range"),
          cleanup: file.cleanupAfterStream,
        },
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const status: ContentfulStatusCode = message.includes("does not exist") ? 404 : 400;
      throw httpError(error, status);
    }
  });

  app.post(
    "/api/assets/upload",
    async (c) => {
      try {
        const tmpDir = await fs.promises.mkdtemp(
          path.join(path.dirname(DIST_DIR), ".upload-"),
        );
        try {
          const upload = await parseAndStageUpload(
            c.req.raw,
            tmpDir,
            maxUploadFileBytes,
            maxUploadTotalBytes,
          );
          const result = await manager.upload(
            upload.volume,
            upload.destination,
            upload.localPaths,
            upload.overwrite,
          );
          return c.json(result);
        } finally {
          await fs.promises.rm(tmpDir, { recursive: true, force: true });
        }
      } catch (error) {
        throw httpError(error, 400);
      }
    },
  );

  app.post("/api/assets/move", async (c) => {
    try {
      const body = await c.req.json<{
        source_volume: string;
        source_path: string;
        destination_volume: string;
        destination_path: string;
        overwrite?: boolean;
      }>();
      const result = await manager.move({
        sourceVolume: body.source_volume,
        sourcePath: body.source_path,
        destinationVolume: body.destination_volume,
        destinationPath: body.destination_path,
        overwrite: body.overwrite,
      });
      return c.json(result);
    } catch (error) {
      throw httpError(error, 400);
    }
  });

  app.post("/api/assets/mkdir", async (c) => {
    try {
      const body = await c.req.json<{
        volume: string;
        path: string;
      }>();
      if (!body.path?.trim()) {
        throw new Error("path is required");
      }
      const result = await manager.mkdir(body.volume, body.path);
      return c.json(result);
    } catch (error) {
      throw httpError(error, 400);
    }
  });

  app.delete("/api/assets", async (c) => {
    try {
      const body = await c.req.json<{
        volume: string;
        path?: string;
        recursive?: boolean;
        items?: { path: string; recursive?: boolean }[];
        workers?: number;
        /** When true (default for multi-delete), stream NDJSON progress lines. */
        stream?: boolean;
      }>();
      if (body.items?.length) {
        const wantStream = body.stream !== false;
        if (!wantStream) {
          const result = await manager.deleteMany(body.volume, body.items, {
            workers: body.workers,
          });
          return c.json(result);
        }

        const encoder = new TextEncoder();
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            const write = (obj: unknown) => {
              controller.enqueue(encoder.encode(`${JSON.stringify(obj)}\n`));
            };
            void manager
              .deleteMany(body.volume, body.items!, {
                workers: body.workers,
                onProgress: (progress) => {
                  write({ type: "progress", ...progress });
                },
              })
              .then((result) => {
                write({ type: "done", ...result });
                controller.close();
              })
              .catch((error: unknown) => {
                const message =
                  error instanceof Error ? error.message : String(error);
                write({ type: "error", detail: message });
                controller.close();
              });
          },
        });
        return new Response(stream, {
          headers: {
            "Content-Type": "application/x-ndjson; charset=utf-8",
            "Cache-Control": "no-store",
          },
        });
      }
      if (!body.path) {
        throw new Error("path or items is required");
      }
      const result = await manager.delete(body.volume, body.path, Boolean(body.recursive));
      return c.json(result);
    } catch (error) {
      throw httpError(error, 400);
    }
  });

  // Serve React production build when present.
  app.get("*", async (c) => {
    const urlPath = new URL(c.req.url).pathname;
    if (urlPath.startsWith("/api/")) {
      return c.json({ detail: "Not found" }, 404);
    }

    if (!fs.existsSync(DIST_DIR)) {
      return c.json(
        {
          detail:
            "Frontend not built. Run: cd web && npm install && npm run build",
          api: "/api/health",
        },
        503,
      );
    }

    const relative = urlPath === "/" ? "index.html" : urlPath.replace(/^\/+/, "");
    const candidate = path.normalize(path.join(DIST_DIR, relative));
    if (!candidate.startsWith(DIST_DIR)) {
      return c.json({ detail: "Invalid path" }, 400);
    }

    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      const data = await fs.promises.readFile(candidate);
      return new Response(data, {
        headers: { "Content-Type": contentTypeFor(candidate) },
      });
    }

    const indexPath = path.join(DIST_DIR, "index.html");
    const data = await fs.promises.readFile(indexPath);
    return new Response(data, {
      headers: { "Content-Type": "text/html; charset=utf-8" },
    });
  });

  return app;
}

function contentTypeFor(filePath: string): string {
  const ext = path.extname(filePath).toLowerCase();
  const map: Record<string, string> = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
  };
  return map[ext] ?? "application/octet-stream";
}
