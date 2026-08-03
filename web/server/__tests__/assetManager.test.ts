import { describe, expect, it } from "vitest";
import { parseHumanSize, sortEntries } from "../lib/assetManager.js";
import { normalizeVolumePath } from "../lib/paths.js";
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

describe("sortEntries", () => {
  it("keeps directories first", () => {
    const entries = [
      {
        volume: "comfy-inputs",
        path: "a.txt",
        name: "a.txt",
        kind: "file" as const,
        size: 1,
        modified_at: "2025-01-01T00:00:00.000Z",
        media_type: "file",
        is_directory: false,
      },
      {
        volume: "comfy-inputs",
        path: "z",
        name: "z",
        kind: "directory" as const,
        size: 0,
        modified_at: "2025-01-01T00:00:00.000Z",
        media_type: "directory",
        is_directory: true,
      },
    ] satisfies AssetEntry[];
    const sorted = sortEntries(entries, "name_asc");
    expect(sorted[0]?.name).toBe("z");
  });
});
