import { describe, expect, it, vi } from "vitest";
import { AssetManager, parseHumanSize, sortEntries } from "../lib/assetManager.js";
import { normalizeVolumePath } from "../lib/paths.js";
import type { PythonAssetBridge } from "../lib/pythonBridge.js";
import type { AssetEntry } from "../lib/types.js";

describe("path validation", () => {
  it("rejects traversal and absolute paths", () => {
    expect(() => normalizeVolumePath("../x")).toThrow(/\.\./);
    expect(() => normalizeVolumePath("/abs")).toThrow(/relative/);
    expect(normalizeVolumePath("a/b")).toBe("a/b");
    expect(normalizeVolumePath("")).toBe("");
  });
});

describe("parseHumanSize", () => {
  it("parses modal CLI sizes", () => {
    expect(parseHumanSize("0 B")).toBe(0);
    expect(parseHumanSize("80.8 KiB")).toBe(Math.round(80.8 * 1024));
    expect(parseHumanSize("1.5 MiB")).toBe(Math.round(1.5 * 1024 * 1024));
  });
});

describe("materialize stream leases", () => {
  it("requests a separate lease for each concurrent consumer", async () => {
    let sequence = 0;
    const call = vi.fn(async () => {
      sequence += 1;
      return {
        path: `/tmp/lease-${sequence}`,
        name: "video.mp4",
        media_type: "video/mp4",
        size: 10,
        etag: "trusted-cache-key",
        cleanup: true,
      };
    });
    const manager = new AssetManager({
      bridge: { call } as unknown as PythonAssetBridge,
    });

    const [first, second] = await Promise.all([
      manager.materialize("comfy-inputs", "video.mp4"),
      manager.materialize("comfy-inputs", "video.mp4"),
    ]);

    expect(call).toHaveBeenCalledTimes(2);
    expect(first.localPath).not.toBe(second.localPath);
    expect(first.etag).toBe("trusted-cache-key");
    expect(first.cleanupAfterStream).toBe(true);
  });
});

function fileEntry(name: string, overrides: Partial<AssetEntry> = {}): AssetEntry {
  return {
    volume: "comfy-inputs",
    path: name,
    name,
    kind: "file",
    size: 1,
    modified_at: "2025-01-01T00:00:00.000Z",
    media_type: "file",
    is_directory: false,
    ...overrides,
  };
}

describe("sortEntries", () => {
  it("keeps directories first", () => {
    const entries = [
      fileEntry("a.txt"),
      fileEntry("z", {
        kind: "directory",
        media_type: "directory",
        is_directory: true,
        size: 0,
      }),
    ] satisfies AssetEntry[];
    const sorted = sortEntries(entries, "name_asc");
    expect(sorted[0]?.name).toBe("z");
  });

  it("sorts by file extension", () => {
    const entries = [
      fileEntry("b.PNG"),
      fileEntry("a.mp4"),
      fileEntry("c.jpg"),
      fileEntry("readme", { media_type: "file" }),
    ];
    const asc = sortEntries(entries, "type_asc").map((e) => e.name);
    expect(asc).toEqual(["readme", "c.jpg", "a.mp4", "b.PNG"]);
    const desc = sortEntries(entries, "type_desc").map((e) => e.name);
    expect(desc).toEqual(["b.PNG", "a.mp4", "c.jpg", "readme"]);
  });
});
