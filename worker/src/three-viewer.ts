import {threeViewerScript} from "./generated/three-viewer.ts";

export const THREE_VIEWER_URI = "ui://split/3d-viewer-v1.html";
export function threeViewerHtml(origin: string, file?: {filename: string; subfolder: string}): string {
  const config = JSON.stringify({origin: new URL(origin).origin, file}).replaceAll("<", "\\u003c");
  return `<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>3Dモデル</title><style>
:root{color-scheme:light dark;font:14px system-ui}*{box-sizing:border-box}body{margin:0;padding:12px}
main{max-width:1100px;margin:auto}header,footer{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
header{margin-bottom:10px}h1{font-size:15px;font-weight:600;overflow-wrap:anywhere;margin:0;flex:1;min-width:120px}
button,a{font:inherit}button{padding:7px 10px;border:1px solid #8886;border-radius:6px;background:transparent;color:inherit;cursor:pointer}
button:active{transform:scale(.97)}button:disabled{opacity:.45;cursor:default}a{color:LinkText;text-underline-offset:3px}
:focus-visible{outline:2px solid #628bff;outline-offset:3px}[hidden]{display:none!important}
#viewport{height:clamp(260px,55vh,560px);width:100%;background:#141a22;border-radius:8px;overflow:hidden}
#viewport canvas{display:block;touch-action:none}footer{padding-top:10px;min-height:38px}#status{flex:1;min-width:160px;font-size:13px;overflow-wrap:anywhere}
body[data-browser]{padding:clamp(12px,3vw,40px)}body[data-browser] #viewport{height:65vh;min-height:260px}
</style><body${file ? " data-browser" : ""}><main><header><h1 id="name">3Dモデル</h1><button id="reset" disabled>視点をリセット</button></header>
<div id="viewport" data-state="empty"></div><footer><span id="status" role="status" aria-live="polite">3Dモデルを読み込み中…</span>
<button id="retry" hidden>再取得</button><a id="save" hidden>元のファイルを保存</a><a id="open" hidden target="_blank" rel="noopener noreferrer">ブラウザで開く</a></footer></main>
<script>window.split3D=${config};</script><script>${threeViewerScript.replaceAll("</script", "<\\/script")}</script></body></html>`;
}
