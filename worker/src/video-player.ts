// Use MCP authentication for bounded playback; large files retain the browser stream.
import {VIDEO_PART_BYTES} from "./media.ts";
export const PLAYER_URI = "ui://split/video-player-v9.html";
export const PREVIOUS_PLAYER_URIS = ["ui://split/video-player-v8.html","ui://split/video-player-v7.html","ui://split/video-player-v6.html","ui://split/video-player-v5.html","ui://split/video-player-v4.html","ui://split/video-player-v3.html","ui://split/video-player-v2.html","ui://split/video-player-v1.html"];
export const PLAYER_MIME = "text/html;profile=mcp-app";
export const playerHtml = (browserOrigin:string, deliveryOrigin?:string) => `<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>生成した動画</title>
<style>
:root{color-scheme:light dark;font:14px system-ui}body{margin:0}video{width:100%;max-height:420px;object-fit:contain;background:#121212;border-radius:8px}
footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;padding:8px 4px}a{color:LinkText}
</style>
<video controls playsinline hidden aria-label="生成した動画"></video>
<footer><span role="status">動画を読み込み中…</span><button hidden>動画を再取得</button><a hidden target="_blank" rel="noopener noreferrer">保存ページを開く</a></footer>
<script>
const player=document.querySelector("video"), label=document.querySelector("span"), retry=document.querySelector("button"), link=document.querySelector("a");
const pending=new Map(), limit=128*1024*1024;
let peer="*", openLinks=false, initialized=false, serverTools=false, lastHeight, rpcId=1, version=0, current, key="", objectUrl;
function send(msg){parent.postMessage({jsonrpc:"2.0",...msg},peer);}
function request(method,params){
  const id=++rpcId;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{pending.delete(id);reject(Error("応答がありません。再取得してください。"));},65000);
    pending.set(id,{resolve,reject,timer});send({id,method,params});
  });
}
function clearVideo(){
  version++;current=undefined;key="";player.onerror=null;player.onloadeddata=null;
  player.pause();player.removeAttribute("src");player.load();player.hidden=true;
  if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=undefined;
  retry.hidden=true;link.hidden=true;link.removeAttribute("href");
}
function setLink(info){
  try{const url=new URL(info.url);
    if(!url.username && !url.password && ((url.origin===${JSON.stringify(new URL(browserOrigin).origin)} && url.pathname==="/view") || (url.origin===${JSON.stringify(deliveryOrigin || null)} && /^\\/media\\/[a-f0-9-]{36}$/.test(url.pathname)))){link.href=url.href;link.hidden=false;}
  }catch{}
}
function show(parts,info){
  const blob=new Blob(parts,{type:info.mimeType || "video/mp4"});
  if(blob.size!==info.size)throw Error("動画のサイズが一致しません。");
  if(objectUrl)URL.revokeObjectURL(objectUrl);
  objectUrl=URL.createObjectURL(blob);player.hidden=false;
  player.onloadeddata=()=>{label.textContent=info.filename;};
  player.onerror=()=>{label.textContent="動画を再生できません。再取得するか保存ページで開いてください。";retry.hidden=false;};
  player.src=objectUrl;
}
function play(){
  const info=current;if(!info)return;
  version++;retry.hidden=true;
  if(info.delivery==="player" && info.size<=limit){void load();return;}
  if(info.delivery_ref){label.textContent="保存ページで開いてください。";return;}
  if(!link.hidden){
    const raw=new URL(link.href);raw.pathname="/api/view";
    label.textContent="動画を読み込み中…";player.hidden=false;
    player.onloadeddata=()=>{label.textContent=info.filename;};
    player.onerror=()=>{
      player.onerror=null;
      if(info.delivery==="link"){label.textContent="動画を再生できません。保存ページで開いてください。";return;}
      void load();
    };
    player.src=raw.href;
  }else if(info.delivery==="link")label.textContent="動画の再生URLを確認できません。";
  else void load();
}
async function load(){
  let info=current;if(!info)return;
  if(info.size>limit){label.textContent="この動画は128 MiBを超えます。保存ページで開いてください。";return;}
  if(!initialized && !window.openai?.callTool){label.textContent="動画を読み込めます。";retry.hidden=false;return;}
  const run=++version,until=Date.now()+110000;retry.hidden=true;label.textContent="動画を取得中…";
  try{
    const parts=[];let offset=0;
    do{
      const args={prompt_id:info.prompt_id,index:info.index??0,byte_offset:offset};
      if(info.delivery_ref)args.delivery_ref=info.delivery_ref;
      const result=await (serverTools || !window.openai?.callTool ? request("tools/call",{name:"read_video_part",arguments:args}) : window.openai.callTool("read_video_part",args));
      if(run!==version)return;
      if(result.isError){
        if(result._meta?.["split/starting"] && Date.now()<until){
          label.textContent="動画を保存したサーバーを起動中…";await new Promise(r=>setTimeout(r,2000));
          if(run!==version)return;continue;
        }
        throw Error(result.content?.find(x=>x.type==="text")?.text || "動画を取得できませんでした。");
      }
      const meta=result.structuredContent,data=result._meta?.["split/videoPart"];
      if(!meta || meta.prompt_id!==info.prompt_id || meta.index!==(info.index??0))throw Error("指定した動画の応答ではありません。");
      if(info.delivery_ref && (meta.delivery_ref?.media_id!==info.delivery_ref.media_id || meta.delivery_ref?.etag!==info.delivery_ref.etag))throw Error("配信コピーの版が変わりました。作品を再取得してください。");
      if(info.delivery==="starting"){
        current=info={...meta,delivery:"player"};setLink(info);
        if(info.size>limit && !link.hidden){play();return;}
      }
      if(!Number.isSafeInteger(info.size) || info.size<=0 || info.size>limit)throw Error("この動画は128 MiBを超えます。保存ページで開いてください。");
      if(meta.filename!==info.filename || meta.mimeType!==(info.mimeType || "video/mp4") || meta.size!==info.size || meta.byte_offset!==offset ||
        typeof data!=="string" || data.length>4*Math.ceil(${VIDEO_PART_BYTES}/3))throw Error("動画の応答が一致しません。");
      const bytes=Uint8Array.from(atob(data),c=>c.charCodeAt(0));offset+=bytes.length;
      if(!bytes.length || bytes.length!==meta.byte_length || offset>info.size || meta.next_offset!==(offset<info.size?offset:null))throw Error("動画データが不完全です。");
      parts.push(bytes);label.textContent="動画を取得中… "+Math.round(offset/info.size*100)+"%";
    }while(offset<info.size || info.delivery==="starting");
    show(parts,info);
  }catch(e){if(run===version){label.textContent=e.message;retry.hidden=false;}}
}
function render(result){
  const info=result.structuredContent;
  if(result.isError){clearVideo();label.textContent=result.content?.find(x=>x.type==="text")?.text || "動画を取得できませんでした。";return;}
  if(!info?.prompt_id)return;
  const nextKey=JSON.stringify([info.prompt_id,info.index,info.filename,info.size,info.delivery_ref?.media_id,info.delivery_ref?.etag]);
  if(key===nextKey)return;
  clearVideo();key=nextKey;current=info;setLink(info);
  if(info.delivery==="chunk"){label.textContent="動画の一部を取得しました。";return;}
  // Previously saved inline results can still hydrate the current resource.
  const data=result._meta?.["split/video"] ?? result.content?.find(x=>x.type==="resource" && x.resource?.mimeType===(info.mimeType || "video/mp4"))?.resource.blob;
  if(info.delivery==="inline" && info.size<=limit && typeof data==="string" && data.length===4*Math.ceil(info.size/3)){
    try{show([Uint8Array.from(atob(data),c=>c.charCodeAt(0))],info);return;}catch{}
  }
  play();
}
retry.onclick=play;
link.onclick=e=>{if(openLinks){e.preventDefault();request("ui/open-link",{url:link.href}).catch(e=>label.textContent=e.message);}};
function legacy(globals){
  const meta=globals?.toolResponseMetadata,result=meta?.mcp_tool_result || meta?.call_tool_result;
  if(result?.structuredContent || result?.isError)render({...result,_meta:result._meta??meta});
  else if(globals?.toolOutput)render({structuredContent:globals.toolOutput,_meta:meta});
}
addEventListener("openai:set_globals",e=>legacy({...window.openai,...e.detail?.globals}));
addEventListener("message",e=>{
  if(e.source!==parent || (peer!=="*" && e.origin!==peer))return;
  const m=e.data;if(!m || m.jsonrpc!=="2.0")return;
  if(m.id===1 && m.result){
    if(e.origin!=="null")peer=e.origin;initialized=true;openLinks=!!m.result.hostCapabilities?.openLinks;serverTools=!!m.result.hostCapabilities?.serverTools;
    send({method:"ui/notifications/initialized",params:{}});resize();
    if(current && !retry.hidden)play();
  }else if(pending.has(m.id) && ("result" in m || "error" in m)){
    const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);
    if(m.error)p.reject(Error(m.error.message));else p.resolve(m.result);
  }else if(m.method==="ui/notifications/tool-result")render(m.params);
  else if(m.method==="ui/resource-teardown"){clearVideo();send({id:m.id,result:{}});}
});
function resize(){const height=document.body.scrollHeight;if(initialized && height!==lastHeight){lastHeight=height;send({method:"ui/notifications/size-changed",params:{height}});}}
new ResizeObserver(resize).observe(document.body);
send({id:1,method:"ui/initialize",params:{protocolVersion:"2026-01-26",appInfo:{name:"split-video",version:"8"},appCapabilities:{}}});
legacy(window.openai);
</script></html>`;
