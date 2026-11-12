// Local-only smoke harness for the real browser and MCP message bridge.
// Run after build:viewer: node --experimental-strip-types scripts/preview-3d.mjs
import {createServer} from "node:http";
import {mediaDocument} from "../src/media-document.ts";
import {threeViewerHtml} from "../src/three-viewer.ts";
import {get3D} from "../src/three-media.ts";
import {texturedGLB, gaussianSPZ} from "../tests/three-fixtures.mjs";

const origin = "http://127.0.0.1:8767", files = {"sample.glb":texturedGLB(),"sample.spz":gaussianSPZ()};
const send = async (path, init) => {
  const url = new URL(path,origin), bytes = files[url.searchParams.get("filename")];
  if (!bytes) return new Response(null,{status:404});
  const range = /^bytes=(\d+)-(\d+)$/.exec(new Headers(init?.headers).get("Range") || "");
  if (!range) return new Response(bytes,{headers:{"Content-Length":String(bytes.length)}});
  const start=Number(range[1]), end=Math.min(Number(range[2])+1,bytes.length);
  if(start>=bytes.length)return new Response(null,{status:416});
  return new Response(bytes.subarray(start,end),{status:206,headers:{"Content-Range":`bytes ${start}-${end-1}/${bytes.length}`,ETag:'"fixture-v1"'}});
};
const server = createServer(async(req,res)=>{
  try {
    const url = new URL(req.url,origin);
    let response;
    if(url.pathname==="/api/view")response=await send(req.url,{headers:req.headers});
    else if(url.pathname==="/rpc"){
      let body="";for await(const part of req)body+=part;
      const {name,args}=JSON.parse(body);
      response=Response.json(await get3D(send,origin,args,name==="read_3d_part"));
    }else if(url.pathname==="/widget"){
      response=new Response(threeViewerHtml(origin),{headers:{"Content-Type":"text/html",
        ...(url.searchParams.has("restricted")?{"Content-Security-Policy":"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src blob: data:; connect-src blob:; worker-src 'none'"}:{})}});
    }else if(url.pathname==="/mcp-test"){
      const filename=url.searchParams.get("filename")==="sample.spz"?"sample.spz":"sample.glb";
      response=new Response(`<!doctype html><title>MCP 3D smoke</title><iframe title="3D MCP viewer" style="width:95vw;height:85vh;border:0" src="/widget${url.searchParams.has("restricted")?"?restricted=1":""}"></iframe><script>
      const frame=document.querySelector('iframe');
      addEventListener('message',async e=>{
        if(e.source!==frame.contentWindow)return;
        const m=e.data,reply=result=>frame.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,result},location.origin);
        if(m.method==='ui/initialize'){
          reply({hostCapabilities:{serverTools:{},openLinks:{}}});
          const r=await fetch('/rpc',{method:'POST',body:JSON.stringify({name:'get_3d',args:{filename:${JSON.stringify(filename)}}})}).then(r=>r.json());
          frame.contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:r},location.origin);
        }else if(m.method==='tools/call')reply(await fetch('/rpc',{method:'POST',body:JSON.stringify({name:m.params.name,args:m.params.arguments})}).then(r=>r.json()));
        else if(m.method==='ui/open-link')window.open(m.params.url);
      });</script>`,{headers:{"Content-Type":"text/html"}});
    }else{
      response=mediaDocument(new Request(url,{headers:{"Sec-Fetch-Dest":"document","Sec-Fetch-Mode":"navigate"}})) || new Response("Not found",{status:404});
    }
    res.writeHead(response.status,Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  }catch(error){console.error(error);res.writeHead(500);res.end("Preview request failed.");}
});
server.listen(8767,"127.0.0.1",()=>console.log("3D smoke: "+origin+"/view?type=output&filename=sample.glb (or sample.spz); /mcp-test?filename=sample.spz"));
