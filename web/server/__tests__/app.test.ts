import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  attachmentContentDisposition,
  createApp,
  createAssetEtag,
} from "../app.js";
import type { AssetManager } from "../lib/assetManager.js";
import { assertLoopbackHost, isLoopbackHost } from "../lib/host.js";
import type { MaterializedFile } from "../lib/types.js";

describe("local-only server binding", () => {
  it("accepts loopback addresses and rejects every non-loopback address", () => {
    expect(isLoopbackHost("127.0.0.1")).toBe(true);
    expect(isLoopbackHost("127.1.2.3")).toBe(true);
    expect(isLoopbackHost("::1")).toBe(true);
    expect(isLoopbackHost("0.0.0.0")).toBe(false);
    expect(isLoopbackHost("192.168.1.10")).toBe(false);
    expect(() => assertLoopbackHost("0.0.0.0")).toThrow(
      /local-only/,
    );
    expect(() => assertLoopbackHost("127.0.0.1")).not.toThrow();
  });
});

describe("asset ETags", () => {
  it("hashes the complete cache key", () => {
    const prefix = `comfy-inputs:${"a".repeat(100)}:`;
    const before = createAssetEtag(`${prefix}2026-08-01:100:640`);
    const after = createAssetEtag(`${prefix}2026-08-02:200:640`);

    expect(before).not.toBe(after);
    expect(before).toMatch(/^"[A-Za-z0-9_-]{43}"$/);
  });
});

describe("download filenames", () => {
  it("provides an RFC 6266 UTF-8 filename parameter", () => {
    const disposition = attachmentContentDisposition("生成結果 (最終).png");

    expect(disposition).toContain('attachment; filename="');
    expect(disposition).toContain(
      "filename*=UTF-8''%E7%94%9F%E6%88%90%E7%B5%90%E6%9E%9C%20%28%E6%9C%80%E7%B5%82%29.png",
    );
    expect(disposition).not.toContain("生成結果");
  });
});

describe("asset streaming", () => {
  async function fixture(
    contents = "0123456789",
    overrides: Partial<MaterializedFile> = {},
  ) {
    const directory = await fs.promises.mkdtemp(
      path.join(os.tmpdir(), "asset-stream-test-"),
    );
    const localPath = path.join(directory, "video.mp4");
    await fs.promises.writeFile(localPath, contents);
    const materialize = vi.fn(async () => ({
      localPath,
      name: "video.mp4",
      mediaType: "video/mp4",
      size: Buffer.byteLength(contents),
      cleanupAfterStream: true,
      ...overrides,
    }));
    const app = createApp({
      manager: { materialize } as unknown as AssetManager,
    });
    return { app, directory, localPath };
  }

  async function expectRemoved(localPath: string): Promise<void> {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      try {
        await fs.promises.access(localPath);
      } catch {
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    throw new Error(`Expected stream lease to be removed: ${localPath}`);
  }

  it("sets the full response length and removes the stream lease", async () => {
    const { app, directory, localPath } = await fixture();
    try {
      const response = await app.request(
        "/api/assets/content?volume=comfy-inputs&path=video.mp4",
      );

      expect(response.status).toBe(200);
      expect(response.headers.get("accept-ranges")).toBe("bytes");
      expect(response.headers.get("content-length")).toBe("10");
      expect(response.headers.get("x-content-type-options")).toBe("nosniff");
      expect(await response.text()).toBe("0123456789");
      await expectRemoved(localPath);
    } finally {
      await fs.promises.rm(directory, { recursive: true, force: true });
    }
  });

  it("serves a byte range with a 206 response", async () => {
    const { app, directory, localPath } = await fixture();
    try {
      const response = await app.request(
        "/api/assets/content?volume=comfy-inputs&path=video.mp4",
        { headers: { Range: "bytes=2-5" } },
      );

      expect(response.status).toBe(206);
      expect(response.headers.get("content-range")).toBe("bytes 2-5/10");
      expect(response.headers.get("content-length")).toBe("4");
      expect(await response.text()).toBe("2345");
      await expectRemoved(localPath);
    } finally {
      await fs.promises.rm(directory, { recursive: true, force: true });
    }
  });

  it("returns 416 and removes the lease for an unsatisfiable range", async () => {
    const { app, directory, localPath } = await fixture();
    try {
      const response = await app.request(
        "/api/assets/content?volume=comfy-inputs&path=video.mp4",
        { headers: { Range: "bytes=20-30" } },
      );

      expect(response.status).toBe(416);
      expect(response.headers.get("content-range")).toBe("bytes */10");
      await expectRemoved(localPath);
    } finally {
      await fs.promises.rm(directory, { recursive: true, force: true });
    }
  });

  it("removes an unused thumbnail lease on a 304 response", async () => {
    const { app, directory, localPath } = await fixture();
    const etag = createAssetEtag("comfy-inputs:video.mp4:10");
    const query = new URLSearchParams({
      volume: "comfy-inputs",
      path: "video.mp4",
      media_type: "video",
    });
    try {
      const response = await app.request(`/api/assets/thumbnail?${query}`, {
        headers: { "If-None-Match": etag },
      });

      expect(response.status).toBe(304);
      await expectRemoved(localPath);
    } finally {
      await fs.promises.rm(directory, { recursive: true, force: true });
    }
  });

  it("ignores a client-supplied filename when selecting Content-Type", async () => {
    const { app, directory } = await fixture();
    try {
      const response = await app.request(
        "/api/assets/content?volume=comfy-inputs&path=video.mp4&name=attack.html",
      );

      expect(response.status).toBe(200);
      expect(response.headers.get("content-type")).toBe("video/mp4");
      await response.arrayBuffer();
    } finally {
      await fs.promises.rm(directory, { recursive: true, force: true });
    }
  });

  it("rejects executable inline media and removes its lease", async () => {
    const { app, directory, localPath } = await fixture("<script></script>", {
      name: "attack.html",
      mediaType: "text/html",
    });
    try {
      const response = await app.request(
        "/api/assets/content?volume=comfy-inputs&path=attack.html",
      );

      expect(response.status).toBe(415);
      await expectRemoved(localPath);
    } finally {
      await fs.promises.rm(directory, { recursive: true, force: true });
    }
  });
});

describe("upload limits", () => {
  it("streams accepted files to a temporary file", async () => {
    const upload = vi.fn(async (_volume, _destination, localPaths: string[]) => ({
      message: await fs.promises.readFile(localPaths[0], "utf8"),
      paths: ["incoming/example.txt"],
    }));
    const manager = { upload } as unknown as AssetManager;
    const app = createApp({
      manager,
      maxUploadFileBytes: 1024,
      maxUploadTotalBytes: 4096,
    });
    const form = new FormData();
    form.set("volume", "comfy-inputs");
    form.set("destination", "incoming");
    form.append("files", new File(["streamed"], "example.txt"));

    const response = await app.request("/api/assets/upload", {
      method: "POST",
      body: form,
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ message: "streamed" });
    expect(upload).toHaveBeenCalledOnce();
  });

  it("rejects a file above the per-file limit", async () => {
    const upload = vi.fn();
    const manager = { upload } as unknown as AssetManager;
    const app = createApp({
      manager,
      maxUploadFileBytes: 3,
      maxUploadTotalBytes: 4096,
    });
    const form = new FormData();
    form.set("volume", "comfy-inputs");
    form.append("files", new File(["four"], "too-large.txt"));

    const response = await app.request("/api/assets/upload", {
      method: "POST",
      body: form,
    });

    expect(response.status).toBe(413);
    expect(upload).not.toHaveBeenCalled();
  });

  it("rejects a multipart request above the total request limit", async () => {
    const upload = vi.fn();
    const manager = { upload } as unknown as AssetManager;
    const app = createApp({
      manager,
      maxUploadFileBytes: 1024,
      maxUploadTotalBytes: 32,
    });
    const form = new FormData();
    form.set("volume", "comfy-inputs");
    form.append("files", new File(["small"], "example.txt"));

    const response = await app.request("/api/assets/upload", {
      method: "POST",
      body: form,
    });

    expect(response.status).toBe(413);
    expect(upload).not.toHaveBeenCalled();
  });
});
