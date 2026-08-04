import fs from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { createApp, createAssetEtag } from "../app.js";
import type { AssetManager } from "../lib/assetManager.js";
import { assertLoopbackHost, isLoopbackHost } from "../lib/host.js";

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
