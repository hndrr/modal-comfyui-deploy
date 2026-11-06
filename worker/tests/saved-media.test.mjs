import assert from "node:assert/strict";
import {test} from "node:test";
import {exportJWK,generateKeyPair,SignJWT} from "jose";
import worker from "../src/index.ts";
import {callSplit} from "../src/remote-mcp.ts";
import {mediaSender,savedOutputRequest} from "../src/saved-media.ts";

const id="fe08cba3-93d1-439f-b5c4-ccb6d9be3421",host="https://split.example.com";
const png=Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=","base64");
const audio=Buffer.from("ID3example");
const video=Buffer.from("0000ftypexample");

test("all saved-output tools bypass ComfyUI readiness and upstream MCP",async()=>{
  const split=()=>{throw new Error("ComfyUI must not be contacted");},paths=[];
  const media=async(path,options)=>{
    paths.push(path);
    if(path.startsWith("/outputs/"))return Response.json({items:[{filename:"a.mp3"}],total:1});
    if(path.startsWith("/history/"))return Response.json({[id]:{status:{status_str:"success",completed:true},
      outputs:{"1":{audio:[{filename:"a.mp3",type:"output"}],video:[{filename:"v.mp4",type:"output"}]}}}});
    assert.equal(new URL(path,host).pathname,"/view");
    return new Response(new URL(path,host).searchParams.get("filename")==="a.mp3"?audio:
      new URL(path,host).searchParams.get("filename")==="v.mp4"?video:png);
  };
  for(const [name,args] of [["get_audio",{prompt_id:id.toUpperCase()}],["read_audio_part",{prompt_id:id.toUpperCase(),byte_offset:0}],
    ["get_video",{prompt_id:id.toUpperCase()}],["get_image",{filename:"画像.png"}],["list_outputs",{prompt_id:id.toUpperCase()}]]) {
    const r=await callSplit(split,name,args,host,undefined,media);
    assert.ok(!r.isError,JSON.stringify(r));
    if(name==="get_image"){
      assert.deepEqual(Buffer.from(r.content[0].data,"base64"),png);
      assert.equal(r.structuredContent.delivery,"inline");
      assert.equal(new URL(r.structuredContent.url).searchParams.get("filename"),"画像.png");
      assert.equal(r.content.some(c=>c.type==="resource_link"),false);
      assert.ok(r.content.find(c=>c.type==="text").text.includes(r.structuredContent.url));
    }
  }
  assert.ok(paths.every(p=>!p.includes("startup")));
  for(const name of ["get_audio","get_video","read_audio_part","get_image","list_outputs"]){
    const r=await callSplit(split,name,{prompt_id:id,filename:"a.png",byte_offset:0},host,undefined,
      ()=>{throw new Error("media unavailable");});
    assert.equal(r.isError,true);assert.match(r.content[0].text,/delivery is unavailable/);
  }
});

test("image links work for small and oversized files, including the legacy ComfyUI route",async()=>{
  const args={filename:"画像 #1.png",subfolder:"render/テスト"},paths=[];
  const send=async path=>{
    paths.push(path);
    if(path==="/split/startup")return Response.json({ready:true});
    const url=new URL(path,host);
    assert.equal(url.pathname,"/view");
    assert.deepEqual(Object.fromEntries(url.searchParams),{type:"output",...args});
    return new Response(png);
  };
  const small=await callSplit(send,"get_image",args,host);
  assert.deepEqual(paths.map(p=>new URL(p,host).pathname),["/split/startup","/view"]);
  assert.equal(small.structuredContent.delivery,"inline");
  assert.match(small.content.find(c=>c.type==="text").text,/https:\/\/split\.example\.com\/view\?/);
  let cancelled=false;
  const large=await callSplit(()=>{throw Error("ComfyUI must not start");},"get_image",args,host,undefined,
    async()=>new Response(new ReadableStream({cancel(){cancelled=true;}}),{headers:{"Content-Length":String(8*1024*1024+1)}}));
  assert.equal(cancelled,true);
  assert.equal(large.structuredContent.delivery,"link");
  assert.equal(large.structuredContent.url,small.structuredContent.url);
  assert.equal(large.content.some(c=>c.type==="image"),false);
  assert.equal(large.content.some(c=>c.type==="resource_link"),false);
  assert.ok(large.content[0].text.includes(small.structuredContent.url));
  const missing=await callSplit(async path=>{
    if(path==="/split/startup")return Response.json({ready:true});
    assert.equal(new URL(path,host).pathname,"/view");
    return new Response("missing",{status:404});
  },"get_image",args,host);
  assert.equal(missing.isError,true);
  assert.match(missing.content[0].text,/HTTP 404/);
});

test("only original output requests qualify, never inputs, transforms or other routes",()=>{
  for(const path of ["/view?filename=a.wav&type=output","/api/view?filename=a.mp4","/view?filename=a.png&utm_source=chat","/view?filename=a.GIF"])
    assert.ok(savedOutputRequest(new Request(host+path)));
  for(const path of ["/view?filename=a.wav&type=input","/view?filename=a.wav&type=temp","/view?filename=a.png&preview=webp",
    "/view?filename=a.png&channel=rgb","/assets/a.js","/models/checkpoints",
    "/view?filename=scene.glb&type=output","/api/view?filename=vector.svg","/view?filename=a.bmp"])
    assert.equal(savedOutputRequest(new Request(host+path)),false);
});

test("fixed media origin accepts read headers only and cannot leak credentials through a redirect",async t=>{
  const cfg={hostname:"split.example.com",origin:"https://delivery.modal.run",key:"media-key",secret:"media-secret"};
  const hits=[];t.mock.method(globalThis,"fetch",async r=>{hits.push(r);return new Response(null,{status:302,headers:{Location:"https://other.example/"}});});
  const credentials=new Headers({"Modal-Key":cfg.key,"Modal-Secret":cfg.secret});
  const send=mediaSender(JSON.stringify({hostname:cfg.hostname,origin:cfg.origin}),cfg.hostname,credentials);
  const result=await send("/view?filename=a.mp4",{headers:{Range:"bytes=1-9","If-Range":'"version"',Cookie:"private",
    Authorization:"secret","CF-Access-Jwt-Assertion":"jwt","CF-Access-Client-Secret":"token","Modal-Key":"attacker"}});
  assert.equal(result.status,302);assert.equal(hits.length,1);
  assert.equal(hits[0].redirect,"manual");
  assert.deepEqual(Object.fromEntries(hits[0].headers),{"if-range":'"version"',"modal-key":"media-key",
    "modal-secret":"media-secret",range:"bytes=1-9","x-modal-execution-mode":"split"});
  for(const path of ["//evil.example/view","/prompt","/_remote_mcp/submit"])
    assert.throws(()=>send(path,{}));
  assert.throws(()=>send("/view",{method:"POST"}));
  for(const origin of ["http://delivery.modal.run","https://delivery.modal.run/path","https://delivery.modal.run.evil.example",
    "https://user:secret@delivery.modal.run","https://delivery.modal.run?x=y"])
    assert.throws(()=>mediaSender(JSON.stringify({...cfg,origin}),cfg.hostname,credentials));
  assert.throws(()=>mediaSender(JSON.stringify(cfg),"other.example.com",credentials));
});

test("OAuth tools and browser bytes use media CPU; discovery and the audio page start no container",async t=>{
  const team="https://saved-media-test.cloudflareaccess.com",origin="https://split.modal.run",delivery="https://media.modal.run";
  const cfg={TEAM_DOMAIN:team,POLICY_AUD:"browser",MODAL_KEY:"split-key",MODAL_SECRET:"split-secret",
    MODAL_ORIGINS:JSON.stringify({"split.example.com":origin}),
    SPLIT_REMOTE_MCP:JSON.stringify({hostname:"split.example.com",split_hostname:"split.example.com",origin,audience:"mcp",owner_email:"owner@example.com"}),
    SPLIT_MEDIA_ORIGIN:JSON.stringify({hostname:"split.example.com",origin:delivery})};
  const {privateKey,publicKey}=await generateKeyPair("RS256");
  const sign=aud=>new SignJWT({type:"app",sub:"owner",email:"owner@example.com"})
    .setProtectedHeader({alg:"RS256",kid:"k"}).setIssuer(team).setAudience(aud).setExpirationTime("5m").sign(privateKey);
  const keys=[{...await exportJWK(publicKey),kid:"k"}],hits=[];
  t.mock.method(globalThis,"fetch",async r=>{
    if(r===team+"/cdn-cgi/access/certs")return Response.json({keys});
    hits.push(r);
    const extension=new URL(r.url).searchParams.get("filename")?.split(".").at(-1);
    assert.equal(new URL(r.url).origin,["glb","svg","bmp"].includes(extension)?origin:delivery);
    assert.equal(r.headers.get("CF-Access-Jwt-Assertion"),null);
    assert.equal(r.headers.get("Cookie"),null);
    if(new URL(r.url).pathname.startsWith("/history/"))return Response.json({[id]:{status:{status_str:"success",completed:true},
      outputs:{"1":{audio:[{filename:"a.mp3",type:"output"}]}}}});
    return new Response(extension==="png"?png:audio);
  });
  const jwt=await sign("mcp"),browser=await sign("browser");
  const rpc=(method,params)=>worker.fetch(new Request(host+"/mcp",{method:"POST",headers:{
    "Content-Type":"application/json",Accept:"application/json, text/event-stream","CF-Access-Jwt-Assertion":jwt},
    body:JSON.stringify({jsonrpc:"2.0",id:1,method,params})}),cfg);
  assert.ok((await (await rpc("tools/list")).json()).result.tools.length);
  assert.equal(hits.length,0);
  const audioRequest=token=>new Request(host+"/view?type=output&filename=a.mp3",{headers:{"CF-Access-Jwt-Assertion":token}});
  assert.equal((await worker.fetch(audioRequest(""),cfg)).status,403);
  assert.equal((await worker.fetch(audioRequest(jwt),cfg)).status,403);
  const page=audioRequest(browser);page.headers.set("Sec-Fetch-Dest","document");page.headers.set("Sec-Fetch-Mode","navigate");
  assert.match(await (await worker.fetch(page,cfg)).text(),/<audio controls/);assert.equal(hits.length,0);
  assert.ok(!(await (await rpc("tools/call",{name:"get_audio",arguments:{prompt_id:id}})).json()).result.isError);
  assert.deepEqual(hits.map(r=>new URL(r.url).pathname),["/history/"+id,"/view"]);
  assert.deepEqual(Buffer.from(await (await worker.fetch(audioRequest(browser),cfg)).arrayBuffer()),audio);
  const image=await (await rpc("tools/call",{name:"get_image",arguments:{filename:"画像.png"}})).json();
  assert.equal(image.result.isError,undefined);
  assert.equal(image.result.structuredContent.delivery,"inline");
  assert.deepEqual(Buffer.from(image.result.content[0].data,"base64"),png);
  assert.ok(image.result.content.find(c=>c.type==="text").text.includes(image.result.structuredContent.url));
  for(const ext of ["gif","glb","svg","bmp"]){
    const req=new Request(host+"/view?type=output&filename=out."+ext,{headers:{"CF-Access-Jwt-Assertion":browser}});
    assert.equal((await worker.fetch(req,cfg)).status,200);
  }
});
