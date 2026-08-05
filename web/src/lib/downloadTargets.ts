import type { AssetEntry } from "../types";

/**
 * Resolve multi-download targets: directories cannot be downloaded as single assets.
 * Returns file entries only, plus how many directories were skipped.
 */
export function filterDownloadableFiles(entries: AssetEntry[]): {
  files: AssetEntry[];
  skippedDirs: number;
} {
  const files: AssetEntry[] = [];
  let skippedDirs = 0;
  for (const entry of entries) {
    if (entry.is_directory) {
      skippedDirs += 1;
    } else {
      files.push(entry);
    }
  }
  return { files, skippedDirs };
}
