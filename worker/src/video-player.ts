// A self-contained MCP Apps view. Playback uses the bytes in the tool result;
// the iframe never contacts Modal or receives Access credentials.
export const PLAYER_URI = "ui://split/video-player-v1.html";
export const PLAYER_MIME = "text/html;profile=mcp-app";
export const playerHtml = `<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>生成した動画</title>
<style>
:root{color-scheme:light dark;font:14px system-ui}body{margin:0}video{width:100%;max-height:65vh;background:#121212;border-radius:8px}
footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;padding:8px 4px}a{color:LinkText}
</style>
<video controls playsinline hidden aria-label="生成した動画"></video>
<footer><span role="status">動画を読み込み中…</span><a hidden target="_blank" rel="noopener noreferrer">ブラウザで開く</a></footer>
<script>
const player = document.querySelector("video"), label = document.querySelector("span"), link = document.querySelector("a");
let objectUrl, peer = "*", openLinks = false, initialized = false, lastHeight;
function send(msg) { parent.postMessage({jsonrpc:"2.0", ...msg}, peer); }
function clearVideo() {
  player.pause(); player.removeAttribute("src"); player.load(); player.hidden = true;
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = null;
}
function render(result) {
  clearVideo(); link.hidden = true; link.removeAttribute("href");
  const info = result.structuredContent || {};
  if (result.isError) { label.textContent = result.content?.find(x => x.type === "text")?.text || "動画を取得できませんでした。"; return; }
  if (info.url) {
    const url = new URL(info.url);
    if (url.protocol === "https:" && url.pathname === "/view") { link.href = url.href; link.hidden = false; }
  }
  const file = result.content?.find(x => x.type === "resource" && x.resource.mimeType === "video/mp4");
  if (!file) { label.textContent = info.delivery === "chunk" ? "動画の一部を取得しました。" : "動画をブラウザで開けます。"; return; }
  const bytes = Uint8Array.from(atob(file.resource.blob), c => c.charCodeAt(0));
  objectUrl = URL.createObjectURL(new Blob([bytes], {type:"video/mp4"}));
  player.src = objectUrl; player.hidden = false; label.textContent = info.filename || "生成した動画";
}
link.onclick = e => {
  if (openLinks) { e.preventDefault(); send({id:2, method:"ui/open-link", params:{url:link.href}}); }
};
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
send({id:1,method:"ui/initialize",params:{protocolVersion:"2026-01-26",appInfo:{name:"split-video",version:"1"},appCapabilities:{}}});
function resize() {
  const height = document.body.scrollHeight;
  if (initialized && height !== lastHeight) { lastHeight = height; send({method:"ui/notifications/size-changed",params:{height}}); }
}
new ResizeObserver(resize).observe(document.body);
</script></html>`;
