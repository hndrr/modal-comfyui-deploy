import assert from "node:assert/strict";
import {test} from "node:test";
import {runInNewContext} from "node:vm";
import {audioPlayerHtml} from "../src/audio-player.ts";

test("ChatGPT globals can mount a cold player and read private audio without standard host tool support", async()=>{
  const id="00000000-0000-4000-8000-000000000001", handlers={}, messages=[], calls=[];
  const elem=()=>({hidden:true,pause(){},load(){},removeAttribute(k){delete this[k];}});
  const elements={audio:elem(),p:elem(),button:elem(),a:elem()};
  const bytes=Uint8Array.from([102,76,97,67]), base={prompt_id:id,index:0};
  const meta={...base,delivery:"inline",filename:"sample.flac",url:"https://split.example/view?filename=sample.flac",
    size:bytes.length,mimeType:"audio/flac",byte_offset:0,byte_length:bytes.length,next_offset:null};
  let finish;
  const played=new Promise(resolve=>{finish=resolve;});
  const parent={postMessage(m){messages.push(m);}},win={openai:{async callTool(name,args){
    calls.push({name,args});
    return {structuredContent:meta,content:[],_meta:{"split/audioPart":Buffer.from(bytes).toString("base64")}};
  }}};
  class BlobURL extends URL {static createObjectURL(blob){finish(blob);return "blob:verified";}}
  const script=audioPlayerHtml.match(/<script>([\s\S]*?)<\/script>/)[1];
  runInNewContext(script,{window:win,parent,document:{querySelector:k=>elements[k],body:{scrollHeight:100}},
    URL:BlobURL,Blob,atob,Uint8Array,setTimeout,clearTimeout,addEventListener:(n,f)=>{handlers[n]=f;},
    ResizeObserver:class{observe(){}}});
  handlers.message({source:parent,origin:"https://host.example",data:{id:1,jsonrpc:"2.0",result:{hostCapabilities:{}}}});
  const result={structuredContent:{...base,delivery:"starting"},content:[{type:"text",text:"Starting"}]};
  handlers["openai:set_globals"]({detail:{globals:{toolResponseMetadata:{mcp_tool_result:result}}}});
  const blob=await played;
  assert.deepEqual(new Uint8Array(await blob.arrayBuffer()),bytes);
  assert.equal(blob.type,meta.mimeType);
  assert.equal(elements.a.href,meta.url);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),[{name:"read_audio_part",args:{...base,byte_offset:0}}]);
  assert.ok(!messages.some(m=>m.method==="tools/call"));
});
