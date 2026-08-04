import { Hono } from "hono";
import { cors } from "hono/cors";
import { HTTPException } from "hono/http-exception";
import type { ContentfulStatusCode } from "hono/utils/http-status";
import fs from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { fileURLToPath } from "node:url";
import { AssetManager, defaultAssetManager } from "./lib/assetManager.js";
import {
  INPUT_VOLUME,
  MODEL_VOLUME,
  OUTPUT_VOLUME,
  SORT_CHOICES,
  VOLUME_LABELS,
  type SortMode,
} from "./lib/types.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(__dirname, "..");
const DIST_DIR = path.join(WEB_ROOT, "dist");

export type AppDeps = {
  manager?: AssetManager;
};

function httpError(error: unknown, status: ContentfulStatusCode = 400): HTTPException {
  const message = error instanceof Error ? error.message : String(error);
  return new HTTPException(status, { message });
}

/** Stream a local file without buffering the whole asset in memory. */
function streamFileResponse(
  localPath: string,
  headers: Record<string, string>,
): Response {
  const nodeStream = fs.createReadStream(localPath);
  const webStream = Readable.toWeb(nodeStream) as ReadableStream;
  return new Response(webStream, { headers });
}

export function createApp(deps: AppDeps = {}) {
  const manager = deps.manager ?? defaultAssetManager;
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
      return streamFileResponse(file.localPath, {
        "Content-Type": file.mediaType,
        ...(download
          ? {
              "Content-Disposition": `attachment; filename="${file.name.replace(/"/g, "")}"`,
            }
          : {}),
      });
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
      const etag = `"${Buffer.from(etagSource).toString("base64url").slice(0, 48)}"`;
      const inm = c.req.header("if-none-match");
      if (inm && inm === etag) {
        return new Response(null, {
          status: 304,
          headers: {
            ETag: etag,
            "Cache-Control": "private, max-age=604800, immutable",
          },
        });
      }
      return streamFileResponse(file.localPath, {
        "Content-Type": file.mediaType || "image/jpeg",
        "Cache-Control": "private, max-age=604800, immutable",
        ETag: etag,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const status: ContentfulStatusCode = message.includes("does not exist") ? 404 : 400;
      throw httpError(error, status);
    }
  });

  app.post("/api/assets/upload", async (c) => {
    try {
      const body = await c.req.parseBody({ all: true });
      const volume = String(body.volume ?? "");
      const destination = String(body.destination ?? "");
      const overwriteRaw = body.overwrite;
      const overwrite =
        overwriteRaw === "true" ||
        overwriteRaw === "1" ||
        (typeof overwriteRaw === "string" && overwriteRaw.toLowerCase() === "on");

      const filesField = body.files;
      const fileList = Array.isArray(filesField)
        ? filesField
        : filesField
          ? [filesField]
          : [];
      if (!fileList.length) throw new Error("Select at least one file to upload.");

      const tmpDir = await fs.promises.mkdtemp(path.join(path.dirname(DIST_DIR), ".upload-"));
      try {
        const localPaths: string[] = [];
        const stagedNames = new Set<string>();
        for (const item of fileList) {
          if (typeof item === "string" || !item || typeof item !== "object") {
            throw new Error("Invalid upload payload.");
          }
          const file = item as File;
          const safeName = path.basename(file.name || "upload.bin");
          if (!safeName || safeName === "." || safeName === "..") {
            throw new Error("Invalid upload filename.");
          }
          if (stagedNames.has(safeName)) {
            throw new Error(
              `Duplicate upload filename in one request: ${safeName}. Rename or upload separately.`,
            );
          }
          stagedNames.add(safeName);
          // Stage each file in its own subdir so basename collisions cannot clobber.
          const stageDir = await fs.promises.mkdtemp(path.join(tmpDir, "f-"));
          const localPath = path.join(stageDir, safeName);
          const buffer = Buffer.from(await file.arrayBuffer());
          await fs.promises.writeFile(localPath, buffer);
          localPaths.push(localPath);
        }
        const result = await manager.upload(volume, destination, localPaths, overwrite);
        return c.json(result);
      } finally {
        await fs.promises.rm(tmpDir, { recursive: true, force: true });
      }
    } catch (error) {
      throw httpError(error, 400);
    }
  });

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
