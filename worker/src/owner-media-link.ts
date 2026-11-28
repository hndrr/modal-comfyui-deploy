// Also embedded in the standalone MCP resources. Keep this predicate self-contained.
export function isOwnerMediaLink(url: URL, linkBase?: string | null): boolean {
  if (!linkBase || !url.href.startsWith(linkBase)) return false;
  const id = url.href.slice(linkBase.length);
  return /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(id);
}
