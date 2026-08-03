export function humanSize(size: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"] as const;
  let value = size;
  for (const unit of units) {
    if (value < 1024 || unit === "TB") {
      return unit === "B" ? `${value} ${unit}` : `${value.toFixed(1)} ${unit}`;
    }
    value /= 1024;
  }
  return `${size} B`;
}

export function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
