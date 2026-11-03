// The MCP view uses tool-result bytes only. Do not send browser credentials,
// fetch /view from the iframe, or publish an output to an unprotected URL.
import {AUDIO_PART_BYTES} from "./media.ts";
export const AUDIO_PLAYER_URI = "ui://split/audio-player-v3.html";
export const PREVIOUS_AUDIO_PLAYER_URIS = ["ui://split/audio-player-v2.html","ui://split/audio-player-v1.html"];
export const audioPlayerHtml = `<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>生成した音声</title><style>:root{color-scheme:light dark;font:14px system-ui}body{margin:0;padding:.5rem}
p{margin:.4rem 0;overflow-wrap:anywhere}footer,#transport:not([hidden]){display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}input{flex:1;min-width:4rem}output{font-variant-numeric:tabular-nums}a{color:LinkText}</style>
<p role="status">音声を読み込み中…</p>
<div id="transport" role="group" aria-label="生成した音声" hidden><button id="play">再生</button><input type="range" min="0" max="0" step="0.1" value="0" aria-label="再生位置" disabled><output>0:00</output></div>
<footer><button hidden>音声を再取得</button><a hidden target="_blank" rel="noopener noreferrer">再生・保存ページを開く</a></footer>
<script>
const transport=document.querySelector("#transport"), play=document.querySelector("#play"), seek=document.querySelector("input"), clock=document.querySelector("output");
const statusLabel=document.querySelector("p"), retry=document.querySelector("footer button"), link=document.querySelector("a");
const autoLoad=4*1024*1024, limit=64*1024*1024, pending=new Map();
let peer="*", serial=1, version=0, current, key="", openLinks=false, ready=false, serverTools=false;
// Play verified bytes directly. An MCP host can forbid media URLs (including blob:) in its iframe.
let file, context, buffer, source, offset=0, started=0, timer;
function time(t){const s=Math.floor(t);return Math.floor(s/60)+":"+String(s%60).padStart(2,"0");}
function position(){return source?Math.min(buffer.duration,offset+context.currentTime-started):offset;}
function tick(){const p=position();seek.value=p;clock.textContent=buffer?time(p)+" / "+time(buffer.duration):"0:00";}
function stop(){if(source){offset=position();source.onended=null;source.stop();source.disconnect();source=null;}
  clearInterval(timer);play.textContent="再生";tick();}
function start(){if(offset>=buffer.duration)offset=0;
  source=context.createBufferSource();source.buffer=buffer;source.connect(context.destination);started=context.currentTime;
  source.onended=()=>{source.disconnect();source=null;offset=buffer.duration;clearInterval(timer);play.textContent="再生";tick();};
  source.start(0,offset);timer=setInterval(tick,200);play.textContent="一時停止";tick();}
function resetPlayer(){stop();if(context)void context.close().catch(()=>{});context=null;file=null;buffer=null;offset=0;
  transport.hidden=true;play.disabled=false;seek.disabled=true;seek.max=0;tick();}
play.onclick=async()=>{
  if(source){stop();return;}if(!file)return;
  const selected=file;play.disabled=true;
  try{context??=new AudioContext();await context.resume();
    if(selected!==file)return;
    if(!buffer){const decoded=await context.decodeAudioData(await selected.arrayBuffer());if(selected!==file)return;buffer=decoded;
      seek.max=buffer.duration;seek.disabled=false;}
    start();
  }catch{if(selected===file)statusLabel.textContent="音声を再生できません。再生・保存ページを開いてください。";}
  finally{if(selected===file)play.disabled=false;}
};
seek.oninput=()=>{if(!buffer)return;const to=Number(seek.value),wasPlaying=!!source;stop();offset=to;
  if(wasPlaying && offset<buffer.duration)start();else tick();};
function send(message){parent.postMessage({jsonrpc:"2.0",...message},peer);}
function request(method,params){
  const id=++serial;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{pending.delete(id);reject(Error("応答がありません。再取得してください。"));},65000);
    pending.set(id,{resolve,reject,timer});send({id,method,params});
  });
}
function clear(){
  version++; retry.hidden=true; link.hidden=true; link.removeAttribute("href"); resetPlayer();current=null;key="";
}
function decode(data){return Uint8Array.from(atob(data),c=>c.charCodeAt(0));}
function show(parts,info){
  const blob=new Blob(parts,{type:info.mimeType});
  if(blob.size!==info.size) throw Error("音声のサイズが一致しません。");
  file=blob;transport.hidden=false;statusLabel.textContent=info.filename;
}
function setLink(info){
  try{const u=new URL(info.url);if(u.protocol==="https:" && u.pathname==="/view"){link.href=u.href;link.hidden=false;}}catch{}
}
function sizePolicy(info,automatic){
  if(!Number.isSafeInteger(info.size) || info.size<=0 || info.size>limit){
    statusLabel.textContent="この音声は再生・保存ページで開けます。";return false;
  }
  if(automatic && info.size>autoLoad){
    retry.textContent="音声を読み込む";statusLabel.textContent=info.filename;retry.hidden=false;return false;
  }
  return true;
}
async function load(automatic=false){
  let info=current;const run=++version, until=Date.now()+110000;
  retry.hidden=true;statusLabel.textContent="音声を取得中…";
  try {
    const parts=[];let offset=0;
    do {
      const args={prompt_id:info.prompt_id,index:info.index,byte_offset:offset};
      const result=await (serverTools || !window.openai?.callTool ? request("tools/call",{name:"read_audio_part",arguments:args}) :
        window.openai.callTool("read_audio_part",args));
      if(run!==version)return;
      if(result.isError){
        if(result._meta?.["split/starting"] && Date.now()<until){
          statusLabel.textContent="音声を保存したサーバーを起動中…";await new Promise(r=>setTimeout(r,2000));
          if(run!==version)return;continue;
        }
        throw Error(result.content?.find(x=>x.type==="text")?.text || "音声を取得できませんでした。");
      }
      const meta=result.structuredContent, data=result._meta?.["split/audioPart"];
      if(!meta || meta.prompt_id!==info.prompt_id || meta.index!==info.index) throw Error("指定した音声の応答ではありません。");
      if(info.delivery==="starting"){
        current=info={...meta,delivery:"player"};setLink(info);
        if(!sizePolicy(info,automatic))return;
      }
      if(meta.filename!==info.filename ||
        meta.mimeType!==info.mimeType || meta.size!==info.size || meta.byte_offset!==offset || typeof data!=="string" ||
        data.length>4*Math.ceil(${AUDIO_PART_BYTES}/3))throw Error("音声の応答が一致しません。");
      const bytes=decode(data);offset+=bytes.length;
      if(!bytes.length || bytes.length!==meta.byte_length || offset>info.size ||
        meta.next_offset!==(offset<info.size?offset:null))throw Error("音声データが不完全です。");
      parts.push(bytes);
    }while(offset<info.size || info.delivery==="starting");
    show(parts,info);
  }catch(e){if(run===version){statusLabel.textContent=e.message;retry.hidden=false;}}
}
function render(result){
  const info=result.structuredContent;
  if(result.isError){clear();statusLabel.textContent=result.content?.find(x=>x.type==="text")?.text || "音声を取得できませんでした。";return;}
  if(!info?.prompt_id)return;
  const k=JSON.stringify([info.prompt_id,info.index,info.filename,info.size]);
  if(k===key)return;
  clear();key=k;current=info;
  setLink(info);
  if(info.delivery!=="starting" && !sizePolicy(info,true))return;
  const inline=result.content?.find(x=>x.type==="audio" && x.mimeType===info.mimeType);
  if(info.delivery==="inline" && inline){try{show([decode(inline.data)],info);return;}catch{}}
  retry.textContent="音声を再取得";
  if(ready || window.openai?.callTool)void load(true);
  else {statusLabel.textContent="音声を取得できます。";retry.hidden=false;}
}
retry.onclick=()=>void load();
link.onclick=e=>{if(openLinks){e.preventDefault();request("ui/open-link",{url:link.href}).catch(err=>statusLabel.textContent=err.message);}};
function legacy(globals){
  const meta=globals?.toolResponseMetadata, result=meta?.mcp_tool_result || meta?.call_tool_result;
  if(result?.structuredContent || result?.isError)render(result);
  else if(globals?.toolOutput)render({structuredContent:globals.toolOutput});
}
addEventListener("openai:set_globals",e=>legacy(e.detail?.globals));
addEventListener("message",e=>{
  if(e.source!==parent || (peer!=="*" && e.origin!==peer))return;
  const m=e.data;if(!m || m.jsonrpc!=="2.0")return;
  if(m.id===1 && m.result){
    if(e.origin!=="null")peer=e.origin;ready=true;openLinks=!!m.result.hostCapabilities?.openLinks;serverTools=!!m.result.hostCapabilities?.serverTools;
    send({method:"ui/notifications/initialized",params:{}});resize();
    if(current && (current.delivery==="starting" || current.size<=autoLoad) && !retry.hidden)void load(true);
  }else if(pending.has(m.id) && ("result" in m || "error" in m)){
    const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);
    if(m.error)p.reject(Error(m.error.message));else p.resolve(m.result);
  }else if(m.method==="ui/notifications/tool-result")render(m.params);
  else if(m.method==="ui/resource-teardown"){clear();send({id:m.id,result:{}});}
});
function resize(){if(ready)send({method:"ui/notifications/size-changed",params:{height:document.body.scrollHeight}});}
new ResizeObserver(resize).observe(document.body);
send({id:1,method:"ui/initialize",params:{protocolVersion:"2026-01-26",appInfo:{name:"split-audio",version:"3"},appCapabilities:{}}});
legacy(window.openai);
</script></html>`;
