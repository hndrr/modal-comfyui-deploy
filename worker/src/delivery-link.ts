// Also embedded in the standalone MCP resources. Keep this predicate self-contained.
export function isDeliveryLink(url: URL, linkBase?: string | null): boolean {
  if (!linkBase || !url.href.startsWith(linkBase)) return false;
  const id = url.href.slice(linkBase.length);
  if (linkBase.endsWith("/s/")) return /^[a-f0-9]{64}$/.test(id);
  return /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(id);
}
