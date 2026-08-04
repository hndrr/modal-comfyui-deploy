export function isLoopbackHost(host: string): boolean {
  const normalized = host.trim().toLowerCase().replace(/^\[|\]$/g, "");
  if (normalized === "localhost" || normalized === "::1") return true;
  if (normalized === "0:0:0:0:0:0:0:1") return true;

  const octets = normalized.split(".");
  return (
    octets.length === 4 &&
    octets.every((octet) => /^\d{1,3}$/.test(octet) && Number(octet) <= 255) &&
    Number(octets[0]) === 127
  );
}

export function assertLoopbackHost(host: string): void {
  if (!isLoopbackHost(host)) {
    throw new Error(
      "The asset manager is local-only. HOST must be a loopback address.",
    );
  }
}
