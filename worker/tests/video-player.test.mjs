import assert from "node:assert/strict";
import {test} from "node:test";
import {runInNewContext} from "node:vm";
import {playerHtml} from "../src/video-player.ts";
import {getMedia,VIDEO_PART_BYTES} from "../src/media.ts";

test("MCP playback avoids browser Access and repeated results preserve the video",async()=>{
  const handlers={},calls=[],messages=[],elements=Object.fromEntries(["video","span","button","a"].map(k=>[k,{hidden:true,
    pause(){},load(){},removeAttribute(k){delete this[k];}}]));
  const bytes=Buffer.alloc(4*1024*1024+9);bytes.write("ftyp",4);
  const info={prompt_id:"00000000-0000-4000-8000-000000000001",index:0,filename:"clip.mp4",mimeType:"video/mp4",
    size:bytes.length,url:"https://split.example.com/view?filename=clip.mp4",delivery:"player"};
  let file,revoked=false;
  class BrowserURL extends URL{
    static createObjectURL(blob){file=blob;return "blob:video";}
    static revokeObjectURL(){revoked=true;}
  }
  const parent={postMessage:m=>messages.push(m)},win={openai:{async callTool(name,args){
    calls.push({name,args});const chunk=bytes.subarray(args.byte_offset,args.byte_offset+VIDEO_PART_BYTES);
    const next=args.byte_offset+chunk.length;
    return {content:[],structuredContent:{...info,delivery:next<bytes.length?"chunk":"inline",byte_offset:args.byte_offset,
      byte_length:chunk.length,next_offset:next<bytes.length?next:null},_meta:{"split/videoPart":chunk.toString("base64")}};
  }}};
  runInNewContext(playerHtml("https://split.example.com").split("<script>")[1].split("</script>")[0],
    {window:win,parent,document:{querySelector:k=>elements[k],body:{scrollHeight:400}},URL:BrowserURL,Blob,Uint8Array,atob,
      setTimeout,clearTimeout,ResizeObserver:class{observe(){}},addEventListener:(n,f)=>handlers[n]=f});
  handlers["openai:set_globals"]({detail:{globals:{toolOutput:info}}});
  await new Promise(r=>setImmediate(r));
  assert.equal(elements.video.hidden,false);assert.equal(elements.video.src,"blob:video");
  assert.deepEqual(Buffer.from(await file.arrayBuffer()),bytes);
  assert.equal(calls.length,9);assert.ok(calls.every(c=>c.name==="read_video_part"));
  handlers["openai:set_globals"]({detail:{globals:{toolOutput:info}}});
  assert.equal(calls.length,9);assert.equal(revoked,false);assert.equal(elements.video.src,"blob:video");
  elements.video.onerror();assert.equal(elements.button.hidden,false);
  elements.button.onclick();await new Promise(r=>setImmediate(r));assert.equal(calls.length,18);
  assert.equal(elements.video.src,"blob:video");
  handlers.message({source:parent,origin:"https://host.example",data:{jsonrpc:"2.0",id:3,method:"ui/resource-teardown"}});
  assert.equal(revoked,true);assert.equal(elements.video.hidden,true);
});

test("HTTPS playback skips private chunks, including videos above the fallback memory limit",async()=>{
  const handlers={},calls=[],elements=Object.fromEntries(["video","span","button","a"].map(k=>[k,{hidden:true,
    pause(){},load(){},removeAttribute(k){delete this[k];}}]));
  const info={prompt_id:"00000000-0000-4000-8000-000000000001",index:0,filename:"clip.mp4",size:128*1024*1024+1,
    delivery:"player",url:"https://split.example.com/view?filename=clip.mp4"};
  runInNewContext(playerHtml("https://split.example.com").split("<script>")[1].split("</script>")[0],
    {window:{openai:{callTool:async name=>{calls.push(name);throw Error("unexpected chunk");}}},parent:{postMessage(){}},
      document:{querySelector:k=>elements[k],body:{scrollHeight:100}},URL,Blob,Uint8Array,atob,setTimeout,clearTimeout,
      ResizeObserver:class{observe(){}},addEventListener:(n,f)=>handlers[n]=f});
  handlers["openai:set_globals"]({detail:{globals:{toolOutput:info}}});
  elements.video.onloadeddata();assert.equal(elements.span.textContent,"clip.mp4");
  assert.equal(elements.video.src,"https://split.example.com/api/view?filename=clip.mp4");
  assert.equal(elements.video.hidden,false);assert.deepEqual(calls,[]);
  elements.video.onerror();await new Promise(r=>setImmediate(r));
  assert.match(elements.span.textContent,/128 MiB/);assert.deepEqual(calls,[]);
});

test("a mismatched private video response is rejected before it can play",async()=>{
  const handlers={},elements=Object.fromEntries(["video","span","button","a"].map(k=>[k,{hidden:true,
    pause(){},load(){},removeAttribute(k){delete this[k];}}]));
  const info={prompt_id:"00000000-0000-4000-8000-000000000001",index:0,filename:"clip.mp4",size:20,delivery:"player"};
  runInNewContext(playerHtml("https://split.example.com").split("<script>")[1].split("</script>")[0],
    {window:{openai:{callTool:async()=>({structuredContent:{...info,index:1}})}},parent:{postMessage(){}},
      document:{querySelector:k=>elements[k],body:{scrollHeight:100}},URL,Blob,Uint8Array,atob,setTimeout,clearTimeout,
      ResizeObserver:class{observe(){}},addEventListener:(n,f)=>handlers[n]=f});
  handlers["openai:set_globals"]({detail:{globals:{toolOutput:info}}});
  await new Promise(r=>setImmediate(r));
  assert.equal(elements.video.src,undefined);assert.equal(elements.button.hidden,false);
  assert.match(elements.span.textContent,/指定した動画/);
});

for (const size of [VIDEO_PART_BYTES+1,4*1024*1024,9000000])
test(`a range-ignoring upstream keeps a ${size}-byte video playable without private chunk retries`,async()=>{
  const id="00000000-0000-4000-8000-000000000001",origin="https://split.example.com";
  const bytes=size<=4*1024*1024 ? Buffer.alloc(size) : null;
  bytes?.write("ftyp",4);
  const result=await getMedia("video",async path=>path.startsWith("/history/") ?
    Response.json({[id]:{status:{status_str:"success",completed:true},outputs:{"1":{video:[{filename:"clip.mp4",type:"output"}]}}}}) :
    new Response(bytes,{status:200,headers:{"Content-Length":String(size)}}),origin,{prompt_id:id});
  assert.equal(result.structuredContent.delivery,"link");assert.equal(result.structuredContent.size,bytes?.length);
  const handlers={},calls=[],elements=Object.fromEntries(["video","span","button","a"].map(k=>[k,{hidden:true,
    pause(){},load(){},removeAttribute(k){delete this[k];}}]));
  runInNewContext(playerHtml(origin).split("<script>")[1].split("</script>")[0],
    {window:{openai:{callTool:async(name)=>{calls.push(name);return {isError:true};}}},parent:{postMessage(){}},
      document:{querySelector:k=>elements[k],body:{scrollHeight:100}},URL,Blob,Uint8Array,atob,setTimeout,clearTimeout,
      ResizeObserver:class{observe(){}},addEventListener:(n,f)=>handlers[n]=f});
  handlers["openai:set_globals"]({detail:{globals:{toolOutput:result.structuredContent}}});
  await new Promise(r=>setImmediate(r));
  assert.deepEqual(calls,[]);assert.equal(elements.video.hidden,false);
  assert.equal(elements.video.src,"https://split.example.com/api/view?type=output&filename=clip.mp4&subfolder=");
  elements.video.onloadeddata();assert.equal(elements.span.textContent,"clip.mp4");
  elements.video.onerror();assert.equal(elements.button.hidden,true);
  assert.match(elements.span.textContent,/保存ページ/);assert.doesNotMatch(elements.span.textContent,/128 MiB/);
  assert.equal(elements.a.hidden,false);assert.deepEqual(calls,[]);
});
