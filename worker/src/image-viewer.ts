// Display tool-result bytes locally; the iframe never fetches an Access-protected image.
export const IMAGE_VIEWER_URI = "ui://split/image-viewer-v1.html";
export const imageViewerHtml = `<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>生成した画像</title><style>:root{color-scheme:light dark;font:14px system-ui}body{margin:0;padding:.5rem}
img{display:block;max-width:100%;max-height:720px;margin:auto;object-fit:contain}img[hidden]{display:none}
footer{display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding:.5rem 0;overflow-wrap:anywhere}a{color:LinkText}</style>
<img hidden alt="生成した画像"><footer><span role="status">画像を読み込み中…</span><a hidden target="_blank" rel="noopener noreferrer">画像を開く・保存する</a></footer>
<script>
const image=document.querySelector("img"),label=document.querySelector("span"),link=document.querySelector("a");
let peer="*",ready=false,openLinks=false;
function send(message){parent.postMessage({jsonrpc:"2.0",...message},peer);}
function clear(){image.hidden=true;image.removeAttribute("src");link.hidden=true;link.removeAttribute("href");}
function render(result){
  clear();
  if(result.isError){label.textContent=result.content?.find(x=>x.type==="text")?.text || "画像を取得できませんでした。";return;}
  const info=result.structuredContent;
  if(!info)return;
  const url=new URL(info.url);
  if(url.protocol==="https:" && url.pathname==="/view"){link.href=url.href;link.hidden=false;}
  label.textContent=info.filename;
  const file=result.content?.find(x=>x.type==="image");
  if(file && ["image/png","image/jpeg","image/webp"].includes(file.mimeType)){
    image.alt=info.filename;image.src="data:"+file.mimeType+";base64,"+file.data;image.hidden=false;
  }else label.textContent=info.filename+"：閲覧リンクから開けます。";
}
image.onerror=()=>{image.hidden=true;label.textContent="画像を表示できません。閲覧リンクから開いてください。";};
link.onclick=e=>{if(openLinks){e.preventDefault();send({id:2,method:"ui/open-link",params:{url:link.href}});}};
function legacy(globals){
  const meta=globals?.toolResponseMetadata,result=meta?.mcp_tool_result || meta?.call_tool_result;
  if(result?.structuredContent || result?.isError)render(result);
  else if(globals?.toolOutput && image.hidden)render({structuredContent:globals.toolOutput});
}
addEventListener("openai:set_globals",e=>legacy(e.detail?.globals));
addEventListener("message",e=>{
  if(e.source!==parent || (peer!=="*" && e.origin!==peer))return;
  const m=e.data;if(!m || m.jsonrpc!=="2.0")return;
  if(m.id===1 && m.result){
    if(e.origin!=="null")peer=e.origin;ready=true;openLinks=!!m.result.hostCapabilities?.openLinks;
    send({method:"ui/notifications/initialized",params:{}});resize();
  }else if(m.method==="ui/notifications/tool-result")render(m.params);
  else if(m.method==="ui/resource-teardown"){clear();send({id:m.id,result:{}});}
});
function resize(){if(ready)send({method:"ui/notifications/size-changed",params:{height:document.body.scrollHeight}});}
new ResizeObserver(resize).observe(document.body);
send({id:1,method:"ui/initialize",params:{protocolVersion:"2026-01-26",appInfo:{name:"split-image",version:"1"},appCapabilities:{}}});
legacy(window.openai);
</script></html>`;
