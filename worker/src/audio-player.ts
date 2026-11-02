// The MCP view uses tool-result bytes only. Do not send browser credentials,
// fetch /view from the iframe, or publish an output to an unprotected URL.
export const AUDIO_PLAYER_URI = "ui://split/audio-player-v1.html";
export const audioPlayerHtml = `<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>生成した音声</title><style>:root{color-scheme:light dark;font:14px system-ui}body{margin:0;padding:.5rem}
audio{width:100%}p{margin:.4rem 0;overflow-wrap:anywhere}footer{display:flex;align-items:center;gap:1rem;flex-wrap:wrap}a{color:LinkText}</style>
<p role="status">音声を読み込み中…</p><audio aria-label="生成した音声" controls preload="metadata" hidden></audio>
<footer><button hidden>音声を再取得</button><a hidden target="_blank" rel="noopener noreferrer">再生・保存ページを開く</a></footer>
<script>
const audio=document.querySelector("audio"), statusLabel=document.querySelector("p"), retry=document.querySelector("button"), link=document.querySelector("a");
const autoLoad=4*1024*1024, limit=64*1024*1024, pending=new Map();
let peer="*", serial=1, version=0, url, current, key="", openLinks=false, ready=false;
function send(message){parent.postMessage({jsonrpc:"2.0",...message},peer);}
function request(method,params){
  const id=++serial;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{pending.delete(id);reject(Error("応答がありません。再取得してください。"));},65000);
    pending.set(id,{resolve,reject,timer});send({id,method,params});
  });
}
function clear(){
  version++; retry.hidden=true; link.hidden=true; link.removeAttribute("href"); audio.pause();
  audio.removeAttribute("src");audio.load();audio.hidden=true;
  if(url) URL.revokeObjectURL(url); url=null;current=null;key="";
}
function decode(data){return Uint8Array.from(atob(data),c=>c.charCodeAt(0));}
function show(parts,info){
  const blob=new Blob(parts,{type:info.mimeType});
  if(blob.size!==info.size) throw Error("音声のサイズが一致しません。");
  url=URL.createObjectURL(blob);audio.src=url;audio.hidden=false;statusLabel.textContent=info.filename;
}
async function load(){
  const info=current, run=++version;
  retry.hidden=true;statusLabel.textContent="音声を取得中…";
  try {
    const parts=[];let offset=0;
    while(offset<info.size){
      const args={prompt_id:info.prompt_id,index:info.index,byte_offset:offset};
      const result=await (ready || !window.openai?.callTool ? request("tools/call",{name:"read_audio_part",arguments:args}) :
        window.openai.callTool("read_audio_part",args));
      if(run!==version)return;
      if(result.isError)throw Error(result.content?.find(x=>x.type==="text")?.text || "音声を取得できませんでした。");
      const meta=result.structuredContent, data=result._meta?.["split/audioPart"];
      if(!meta || meta.prompt_id!==info.prompt_id || meta.index!==info.index || meta.filename!==info.filename ||
        meta.mimeType!==info.mimeType || meta.size!==info.size || meta.byte_offset!==offset || typeof data!=="string" ||
        data.length>4*Math.ceil(512*1024/3))throw Error("音声の応答が一致しません。");
      const bytes=decode(data);offset+=bytes.length;
      if(!bytes.length || bytes.length!==meta.byte_length || offset>info.size ||
        meta.next_offset!==(offset<info.size?offset:null))throw Error("音声データが不完全です。");
      parts.push(bytes);
    }
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
  try{const u=new URL(info.url);if(u.protocol==="https:" && u.pathname==="/view"){link.href=u.href;link.hidden=false;}}catch{}
  if(!Number.isSafeInteger(info.size) || info.size<=0 || info.size>limit){
    statusLabel.textContent="この音声は再生・保存ページで開けます。";return;
  }
  const inline=result.content?.find(x=>x.type==="audio" && x.mimeType===info.mimeType);
  if(info.delivery==="inline" && inline){try{show([decode(inline.data)],info);return;}catch{}}
  if(info.size>autoLoad){retry.textContent="音声を読み込む";statusLabel.textContent=info.filename;retry.hidden=false;return;}
  retry.textContent="音声を再取得";
  if(ready || window.openai?.callTool)void load();
  else {statusLabel.textContent="音声を取得できます。";retry.hidden=false;}
}
retry.onclick=()=>void load();
audio.onerror=()=>{if(url)statusLabel.textContent="このブラウザで再生できない音声です。再生・保存ページから元ファイルを保存できます。";};
link.onclick=e=>{if(openLinks){e.preventDefault();request("ui/open-link",{url:link.href}).catch(err=>statusLabel.textContent=err.message);}};
function legacy(globals){if(globals?.toolOutput)render({structuredContent:globals.toolOutput});}
addEventListener("openai:set_globals",e=>legacy(e.detail?.globals));
addEventListener("message",e=>{
  if(e.source!==parent || (peer!=="*" && e.origin!==peer))return;
  const m=e.data;if(!m || m.jsonrpc!=="2.0")return;
  if(m.id===1 && m.result){
    if(e.origin!=="null")peer=e.origin;ready=true;openLinks=!!m.result.hostCapabilities?.openLinks;
    send({method:"ui/notifications/initialized",params:{}});resize();
    if(current && current.size<=autoLoad && !retry.hidden)void load();
  }else if(pending.has(m.id) && ("result" in m || "error" in m)){
    const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);
    if(m.error)p.reject(Error(m.error.message));else p.resolve(m.result);
  }else if(m.method==="ui/notifications/tool-result")render(m.params);
  else if(m.method==="ui/resource-teardown"){clear();send({id:m.id,result:{}});}
});
function resize(){if(ready)send({method:"ui/notifications/size-changed",params:{height:document.body.scrollHeight}});}
new ResizeObserver(resize).observe(document.body);
send({id:1,method:"ui/initialize",params:{protocolVersion:"2026-01-26",appInfo:{name:"split-audio",version:"1"},appCapabilities:{}}});
legacy(window.openai);
</script></html>`;
