// The browser handles Access cookies. The player never receives credentials.
export const PLAYER_URI = "ui://split/video-player-v4.html";
export const PREVIOUS_PLAYER_URIS = ["ui://split/video-player-v3.html","ui://split/video-player-v2.html","ui://split/video-player-v1.html"];
export const PLAYER_MIME = "text/html;profile=mcp-app";
export const playerHtml = (browserOrigin:string) => `<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>生成した動画</title>
<style>
:root{color-scheme:light dark;font:14px system-ui}body{margin:0}video{width:100%;max-height:420px;object-fit:contain;background:#121212;border-radius:8px}
footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;padding:8px 4px}a{color:LinkText}
</style>
<video controls playsinline hidden aria-label="生成した動画"></video>
<footer><span role="status">動画を読み込み中…</span><a hidden target="_blank" rel="noopener noreferrer">再生・保存ページを開く</a></footer>
<script>
const player = document.querySelector("video"), label = document.querySelector("span"), link = document.querySelector("a");
let peer = "*", openLinks = false, initialized = false, lastHeight, rpcId = 2, current, inline;
function send(msg) { parent.postMessage({jsonrpc:"2.0", ...msg}, peer); }
function clearVideo() {
  current = undefined; inline = undefined; player.onerror = null;
  player.pause(); player.removeAttribute("src"); player.load(); player.hidden = true;
  link.hidden = true; link.removeAttribute("href");
}
function render(result) {
  const info = result.structuredContent;
  const data = result._meta?.["split/video"] ?? result.content?.find(x => x.type === "resource" && x.resource?.mimeType === "video/mp4")?.resource.blob;
  const hasData = info?.size && typeof data === "string" && data.length === 4 * Math.ceil(info.size / 3);
  if (!result.isError && current && info?.delivery === "inline" && current === info.url) {
    if (hasData) inline = data;
    return;
  }
  clearVideo();
  if (result.isError) { label.textContent = result.content?.find(x => x.type === "text")?.text || "動画を取得できませんでした。"; return; }
  try {
    const url = new URL(info?.url);
    if (url.origin === ${JSON.stringify(new URL(browserOrigin).origin)} && url.pathname === "/view" && !url.username && !url.password) { link.href = url.href; link.hidden = false; }
  } catch {}
  const unavailable = () => { current = undefined; player.hidden = true; label.textContent = link.hidden ? "動画を表示できません。" : "チャット内で再生できません。再生・保存ページを開いてください。"; };
  if (info?.delivery === "chunk") { label.textContent = "動画の一部を取得しました。"; return; }
  if (info?.delivery === "link" && !link.hidden) { label.textContent = "再生・保存ページで動画を開けます。"; return; }
  if (info?.delivery !== "inline" || (link.hidden && !hasData)) { unavailable(); return; }
  current = info.url; inline = hasData ? data : undefined;
  // Some hosts block data: media. Prefer the existing signed-in HTTPS byte route.
  // If browser Access login is unavailable, try the bytes from the MCP result once.
  const useBytes = () => { player.onerror = unavailable;
    if (inline) player.src = "data:video/mp4;base64," + inline; else unavailable(); };
  player.hidden = false; label.textContent = info.filename;
  if (link.hidden) { useBytes(); return; }
  const raw = new URL(link.href); raw.pathname = "/api/view";
  player.onerror = useBytes; player.src = raw.href;
}
link.onclick = e => {
  if (openLinks) { e.preventDefault(); send({id:rpcId++, method:"ui/open-link", params:{url:link.href}}); }
};
function legacy(globals) {
  const meta = globals?.toolResponseMetadata, result = meta?.mcp_tool_result || meta?.call_tool_result;
  if (result?.structuredContent || result?.isError) render({...result, _meta:result._meta ?? meta});
  else if (globals?.toolOutput) render({structuredContent:globals.toolOutput, _meta:meta});
}
addEventListener("openai:set_globals", e => legacy({...window.openai, ...e.detail?.globals}));
addEventListener("message", e => {
  if (e.source !== parent || (peer !== "*" && e.origin !== peer)) return;
  const m = e.data;
  if (!m || m.jsonrpc !== "2.0") return;
  if (m.id === 1 && m.result) {
    if (e.origin !== "null") peer = e.origin;
    initialized = true; openLinks = !!m.result.hostCapabilities?.openLinks;
    send({method:"ui/notifications/initialized",params:{}}); resize();
  } else if (m.method === "ui/notifications/tool-result") { render(m.params); }
  else if (m.method === "ui/resource-teardown") { clearVideo(); send({id:m.id,result:{}}); }
});
send({id:1,method:"ui/initialize",params:{protocolVersion:"2026-01-26",appInfo:{name:"split-video",version:"4"},appCapabilities:{}}});
function resize() {
  const height = document.body.scrollHeight;
  if (initialized && height !== lastHeight) { lastHeight = height; send({method:"ui/notifications/size-changed",params:{height}}); }
}
new ResizeObserver(resize).observe(document.body);
legacy(window.openai);
</script></html>`;
