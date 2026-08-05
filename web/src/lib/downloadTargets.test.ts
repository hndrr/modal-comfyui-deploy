import { describe, expect, it } from "vitest";
import { filterDownloadableFiles } from "./downloadTargets";
import type { AssetEntry } from "../types";

function entry(partial: Partial<AssetEntry> & Pick<AssetEntry, "path" | "is_directory">): AssetEntry {
  return {
    volume: "comfy-outputs",
    name: partial.path.split("/").pop() ?? partial.path,
    kind: partial.is_directory ? "directory" : "file",
    size: 0,
    modified_at: "2026-01-01T00:00:00Z",
    media_type: "other",
    ...partial,
  };
}

describe("filterDownloadableFiles", () => {
  it("returns only files and counts skipped directories", () => {
    const result = filterDownloadableFiles([
      entry({ path: "a.png", is_directory: false }),
      entry({ path: "folder", is_directory: true }),
      entry({ path: "b.mp4", is_directory: false }),
      entry({ path: "nested", is_directory: true }),
    ]);
    expect(result.files.map((f) => f.path)).toEqual(["a.png", "b.mp4"]);
    expect(result.skippedDirs).toBe(2);
  });

  it("returns empty files when only directories are selected", () => {
    const result = filterDownloadableFiles([
      entry({ path: "only-dir", is_directory: true }),
    ]);
    expect(result.files).toEqual([]);
    expect(result.skippedDirs).toBe(1);
  });

  it("returns empty when given no entries", () => {
    expect(filterDownloadableFiles([])).toEqual({ files: [], skippedDirs: 0 });
  });
});
