// Top-level links need an HTML document. ComfyUI media elements and API clients
// keep /view's original byte and Range semantics.
import {threeViewerHtml} from "./three-viewer.ts";
import {threeInputs} from "./three-media.ts";
import {z} from "zod";
export function mediaDocument(request:Request):Response|null {
  const url=new URL(request.url), q=url.searchParams;
  if (url.pathname!=="/view" || request.method!=="GET" || request.headers.has("Range") ||
      request.headers.get("Sec-Fetch-Dest")!=="document" || request.headers.get("Sec-Fetch-Mode")!=="navigate" ||
      q.get("type")!=="output" || q.has("preview") || q.has("channel")) return null;
  const file=q.get("filename") || "", folder=q.get("subfolder") || "";
  if (/\.(?:glb|spz)$/i.test(file)) {
    const parsed = z.object(threeInputs).safeParse({filename:file,subfolder:folder});
    if (!parsed.success) return null;
    return new Response(threeViewerHtml(url.origin,{filename:file,subfolder:folder}),{headers:{
      "Content-Type":"text/html; charset=utf-8","Cache-Control":"private, no-store",
      "Vary":"Sec-Fetch-Dest, Sec-Fetch-Mode, Range",
      "Content-Security-Policy":"default-src 'none'; script-src 'unsafe-inline' 'wasm-unsafe-eval'; worker-src blob:; connect-src 'self' blob: data:; img-src blob: data:; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
      "X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer",
    }});
  }
  const safe=(v:string)=> v && v.length<=255 && v!=="." && v!==".." && !/[/\\:\x00-\x1f\x7f]/.test(v);
  if (!safe(file) || !/\.(?:mp3|wav|flac|ogg|opus|mp4)$/i.test(file) || folder.length>1024 ||
      (folder && !folder.split("/").every(safe))) return null;
  const esc=(s:string)=>s.replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")
    .replaceAll('"',"&quot;").replaceAll("'","&#39;");
  // /api/view is the same ComfyUI byte endpoint. A download remains a file even
  // on browsers that navigate to the download before saving it.
  const src=esc("/api/view?"+new URLSearchParams({type:"output",filename:file,subfolder:folder}));
  const video=/\.mp4$/i.test(file), tag=video?"video":"audio";
  return new Response(`<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(file)}</title><style>:root{color-scheme:light dark;font:16px system-ui}body{max-width:42rem;margin:10vh auto;padding:1.5rem}h1{font-size:1.2rem;overflow-wrap:anywhere}audio,video{width:100%;margin:1rem 0}video{max-height:65vh}</style>
<main><h1>${esc(file)}</h1><${tag} controls${video?" playsinline":""} preload="metadata" src="${src}"></${tag}><p><a href="${src}" download="${esc(file)}">元の${video?"動画":"音声"}を保存</a></p></main></html>`,{headers:{
    "Content-Type":"text/html; charset=utf-8","Cache-Control":"private, no-store","Vary":"Sec-Fetch-Dest, Sec-Fetch-Mode, Range",
    "Content-Security-Policy":"default-src 'none'; media-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer",
  }});
}
