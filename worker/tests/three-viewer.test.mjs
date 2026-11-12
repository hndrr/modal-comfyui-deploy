import assert from "node:assert/strict";
import {test} from "node:test";
import {runInNewContext} from "node:vm";
import {build} from "esbuild";
import {texturedGLB, gaussianSPZ} from "./three-fixtures.mjs";
import {checkGLB} from "../viewer/render.ts";
import {get3D, THREE_PART_BYTES} from "../src/three-media.ts";
import {callSplit} from "../src/remote-mcp.ts";

const origin="https://split.example.com";
const compiled=await build({entryPoints:[new URL("../viewer/app.ts",import.meta.url).pathname],bundle:true,write:false,
  format:"iife",platform:"browser",plugins:[{name:"renderer-stub",setup(b){
    b.onResolve({filter:/\.\/render\.ts$/},()=>({path:"renderer",namespace:"stub"}));
    b.onLoad({filter:/.*/,namespace:"stub"},()=>({contents:`export function createViewer(){return {
      async load(bytes,format){await onDraw(bytes,format)},reset(){},dispose(){onDispose()}}}`,loader:"js"}));
  }}]});
function app(info,{chunk,draw,browser=false,legacy=true,fetch:fetcher}={}){
  const handlers={},calls=[],messages=[],drawn=[],elements=Object.fromEntries(
    ["#viewport","#status","#name","#reset","#retry","#open","#save"].map(id=>[id,{
      hidden:true,dataset:{},removeAttribute(key){delete this[key];}}]));
  let disposed=0;
  const parent={postMessage:m=>messages.push(m)},win={split3D:{origin,...(browser ? {file:{filename:info.filename,subfolder:info.subfolder}} : {})},openai:{callTool:async(name,args)=>{
    calls.push({name,args});return chunk(args);
  }}};
  if(!legacy)delete win.openai;
  runInNewContext(compiled.outputFiles[0].text,{window:win,parent,document:{querySelector:id=>elements[id],body:{scrollHeight:400}},
    addEventListener:(name,fn)=>handlers[name]=fn,ResizeObserver:class{observe(){}},URL,URLSearchParams,Blob,Uint8Array,atob,
    AbortController,setTimeout,clearTimeout,fetch:fetcher || (()=>{throw Error("Inline viewer must not fetch Access URLs");}),
    onDraw:async(bytes,format)=>{await draw?.();drawn.push({bytes:Buffer.from(bytes),format});},onDispose:()=>disposed++});
  const render=value=>handlers["openai:set_globals"]({detail:{globals:{toolOutput:value}}});
  if(!browser)render(info);
  return {handlers,calls,messages,elements,drawn,parent,render,disposed:()=>disposed};
}
const settle=async()=>{for(let i=0;i<10;i++)await new Promise(r=>setImmediate(r));};

test("slow decoding is not timed out and save bytes are copied only on demand",async(t)=>{
  t.mock.timers.enable({apis:["setTimeout"]});
  let finish;
  const decoding=new Promise(resolve=>finish=resolve);
  const info={filename:"mesh.glb",subfolder:"",format:"glb",size:10,delivery:"viewer",etag:'"version"',url:origin+"/view?filename=mesh.glb"};
  const view=app(info,{chunk:()=>({structuredContent:{...info,byte_offset:0,byte_length:10,next_offset:null},
    _meta:{"split/3dPart":Buffer.alloc(10).toString("base64")}}),draw:()=>decoding});
  await settle();t.mock.timers.tick(60_000);await settle();
  assert.equal(view.disposed(),0);assert.equal(view.elements["#viewport"].dataset.state,"loading");
  assert.equal(view.elements["#save"].href,info.url);
  finish();await settle();assert.equal(view.elements["#viewport"].dataset.state,"ready");
  view.elements["#save"].onclick();assert.match(view.elements["#save"].href,/^blob:/);
  view.handlers.message({source:view.parent,origin:"https://host.example",data:{jsonrpc:"2.0",id:2,method:"ui/resource-teardown"}});
});

test("a host teardown sharing a pending tool id is handled as a request",async()=>{
  const view=app(undefined,{legacy:false});
  view.handlers.message({source:view.parent,origin:"https://host.example",data:{jsonrpc:"2.0",id:1,result:{hostCapabilities:{serverTools:{}}}}});
  view.render({filename:"mesh.glb",subfolder:"",format:"glb",size:10,delivery:"viewer",etag:'"version"',url:origin+"/view?filename=mesh.glb"});
  const request=view.messages.find(x=>x.method==="tools/call");assert.ok(request);
  view.handlers.message({source:view.parent,origin:"https://host.example",data:{jsonrpc:"2.0",id:request.id,method:"ui/resource-teardown"}});
  await settle();
  assert.ok(view.messages.some(x=>x.id===request.id && "result" in x));
  assert.equal(view.elements["#viewport"].dataset.state,"empty");assert.equal(view.drawn.length,0);
});

test("private parts are assembled without browser authentication, repeats do not reload, teardown disposes",async()=>{
  const bytes=Buffer.alloc(THREE_PART_BYTES+17,0x61);
  const info={filename:"mesh.glb",subfolder:"3d",format:"glb",size:bytes.length,delivery:"viewer",etag:'"first"',
    url:origin+"/view?filename=mesh.glb&subfolder=3d&type=output"};
  const view=app(info,{chunk:args=>{
    const part=bytes.subarray(args.byte_offset,args.byte_offset+THREE_PART_BYTES),next=args.byte_offset+part.length;
    return {structuredContent:{...info,etag:args.etag,byte_offset:args.byte_offset,byte_length:part.length,next_offset:next<bytes.length?next:null},
      content:[],_meta:{"split/3dPart":part.toString("base64")}};
  }});
  await settle();
  assert.equal(view.drawn.length,1);assert.deepEqual(view.drawn[0].bytes,bytes);
  assert.equal(view.elements["#viewport"].dataset.state,"ready");
  assert.equal(view.elements["#reset"].disabled,false);
  assert.equal(view.calls.length,2);assert.ok(view.calls.every(x=>x.name==="read_3d_part"));
  assert.ok(view.calls.every(x=>x.args.etag===info.etag));
  assert.equal(view.elements["#save"].href,info.url);
  view.elements["#save"].onclick();
  assert.match(view.elements["#save"].href,/^blob:/);
  view.render(info);await settle();assert.equal(view.calls.length,2);
  view.render({...info,etag:'"replacement"'});await settle();
  assert.equal(view.calls.length,4);assert.equal(view.drawn.length,2);
  assert.ok(view.calls.slice(2).every(x=>x.args.etag==='"replacement"'));
  view.handlers.message({source:view.parent,origin:"https://host.example",data:{jsonrpc:"2.0",id:9,method:"ui/resource-teardown"}});
  assert.equal(view.disposed(),2);assert.equal(view.elements["#reset"].disabled,true);
  assert.equal(view.elements["#save"].href,undefined);
});

test("large files, corrupt chunks and blocked renderers retain the signed-in browser fallback",async()=>{
  const info={filename:"mesh.spz",subfolder:"",format:"spz",size:10,delivery:"viewer",url:origin+"/view?filename=mesh.spz"};
  const large=app({...info,size:128*1024*1024+1});await settle();
  assert.equal(large.calls.length,0);assert.match(large.elements["#status"].textContent,/128 MiB/);
  assert.equal(large.elements["#open"].href,info.url);
  const corrupt=app(info,{chunk:()=>({structuredContent:{...info,filename:"other.spz"}})});await settle();
  assert.equal(corrupt.drawn.length,0);assert.equal(corrupt.elements["#viewport"].dataset.state,"error");
  assert.match(corrupt.elements["#status"].textContent,/ブラウザ/);
  const blocked=app(info,{chunk:()=>({structuredContent:{...info,byte_offset:0,byte_length:10,next_offset:null},
    _meta:{"split/3dPart":Buffer.alloc(10).toString("base64")}}),draw:()=>{throw Error("描画できません。");}});
  await settle();assert.equal(blocked.disposed(),1);assert.equal(blocked.elements["#open"].href,info.url);
  assert.equal(blocked.elements["#retry"].hidden,false);
  const changed=app({...info,etag:'"original"'},{chunk:()=>({structuredContent:{...info,etag:'"replaced"',byte_offset:0,byte_length:10,next_offset:null},
    _meta:{"split/3dPart":Buffer.alloc(10).toString("base64")}})});
  await settle();assert.equal(changed.drawn.length,0);assert.equal(changed.elements["#viewport"].dataset.state,"error");
});

test("hosts without serverTools or legacy callTool immediately show the browser link",async()=>{
  const info={filename:"mesh.glb",subfolder:"",format:"glb",size:32,delivery:"viewer",etag:'"version"',url:origin+"/view?filename=mesh.glb"};
  for(const resultFirst of [true,false]){
    const view=app(resultFirst?info:undefined,{legacy:false});
    view.handlers.message({source:view.parent,origin:"https://host.example",data:{jsonrpc:"2.0",id:1,result:{hostCapabilities:{}}}});
    if(!resultFirst)view.render(info);
    await settle();
    assert.equal(view.messages.some(x=>x.method==="tools/call"),false);
    assert.equal(view.drawn.length,0);assert.equal(view.elements["#retry"].hidden,true);
    assert.match(view.elements["#status"].textContent,/このホスト.*ブラウザ/);
    assert.equal(view.elements["#open"].href,info.url);assert.equal(view.elements["#open"].hidden,false);
  }
});

test("browser reads are bounded even without a truthful Content-Length and keep the save link",async()=>{
  const info={filename:"mesh.glb",subfolder:"3d"},limit=128*1024*1024;
  const bytes=texturedGLB(),small=app(info,{browser:true,fetch:async()=>new Response(bytes)});
  await settle();assert.deepEqual(small.drawn[0].bytes,bytes);
  for(const length of [String(limit+1),undefined,"1"]){
    let cancelled=false,pulls=0;
    const body=new ReadableStream({pull(c){pulls++;c.enqueue(new Uint8Array(1024*1024));},cancel(){cancelled=true;}});
    const view=app(info,{browser:true,fetch:async()=>new Response(body,{headers:length?{"Content-Length":length}:{}})});
    await settle();
    assert.equal(cancelled,true);assert.ok(pulls<=(length===String(limit+1)?1:130));
    assert.equal(view.drawn.length,0);assert.equal(view.elements["#reset"].disabled,true);
    assert.match(view.elements["#status"].textContent,/128 MiB/);
    assert.equal(view.elements["#save"].hidden,false);
    assert.match(view.elements["#save"].href,/\/api\/view\?/);
  }
});

test("GLB loader rejects external dependencies before any loader requests can run",()=>{
  for(const dataUris of [false,true]){
    const bytes=texturedGLB({dataUris});
    assert.doesNotThrow(()=>checkGLB(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)));
    for(const offset of [0,4,8]){
      const corrupt=Buffer.from(bytes);corrupt.writeUInt32LE(0,offset);
      assert.throws(()=>checkGLB(corrupt.buffer.slice(corrupt.byteOffset,corrupt.byteOffset+corrupt.byteLength)),/GLB/);
    }
  }
  const original=texturedGLB();
  assert.doesNotThrow(()=>checkGLB(original.buffer.slice(original.byteOffset,original.byteOffset+original.byteLength)));
  for(const uri of ["https://elsewhere.example/texture.png","../texture.png","//elsewhere.example/model.bin"]){
    const text=Buffer.from(JSON.stringify({asset:{version:"2.0"},images:[{uri}]}));
    const buffer=Buffer.alloc(20+text.length);buffer.write("glTF");buffer.writeUInt32LE(2,4);buffer.writeUInt32LE(buffer.length,8);
    buffer.writeUInt32LE(text.length,12);buffer.write("JSON",16);text.copy(buffer,20);
    assert.throws(()=>checkGLB(buffer.buffer.slice(buffer.byteOffset,buffer.byteOffset+buffer.byteLength)),/外部/);
  }
  assert.throws(()=>checkGLB(new ArrayBuffer(0)));
});

test("an initial retrieval error tells the user to rerun the tool without a dead retry button",async()=>{
  const view=app(undefined);
  view.handlers["openai:set_globals"]({detail:{globals:{toolResponseMetadata:{mcp_tool_result:{
    isError:true,content:[{type:"text",text:"Delivery unavailable"}]
  }}}}});
  await settle();
  assert.match(view.elements["#status"].textContent,/get_3d/);
  assert.equal(view.elements["#retry"].hidden,true);
  assert.equal(view.elements["#open"].hidden,true);
  assert.equal(view.calls.length,0);
});

test("get_3d and private reads bypass startup with either CPU route",async()=>{
  for(const [filename,bytes] of [["sample.glb",texturedGLB()],["sample.spz",gaussianSPZ()]]){
    const hits=[];
    const send=async(path,init)=>{
      hits.push(path);const range=/bytes=(\d+)-(\d+)/.exec(new Headers(init.headers).get("Range"));
      const start=Number(range[1]),end=Math.min(Number(range[2])+1,bytes.length);
      if(new Headers(init.headers).has("If-Range"))assert.equal(new Headers(init.headers).get("If-Range"),'"fixture"');
      return new Response(bytes.subarray(start,end),{status:206,headers:{"Content-Range":`bytes ${start}-${end-1}/${bytes.length}`,ETag:'"fixture"'}});
    };
    const split=()=>{throw Error("Must not start ComfyUI");};
    const result=await callSplit(split,"get_3d",{filename},origin,undefined,send);
    assert.equal(result.isError,undefined);assert.equal(result.structuredContent.delivery,"viewer");
    const part=await callSplit(split,"read_3d_part",{filename,byte_offset:0},origin,undefined,send);
    assert.deepEqual(Buffer.from(part._meta["split/3dPart"],"base64"),bytes);
    assert.ok(hits.every(x=>x.startsWith("/view?")));
    assert.equal(hits.length,2);
    const read=await callSplit(send,"get_3d",{filename},origin);
    assert.equal(read.isError,undefined);assert.equal(hits.length,3);
    assert.ok(hits.every(x=>x.startsWith("/view?")));
    const absent=await get3D(async()=>new Response(null,{status:404}),origin,{filename});
    assert.equal(absent.isError,true);
  }
});
