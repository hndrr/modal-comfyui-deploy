import assert from "node:assert/strict";
import {test} from "node:test";
import {runInNewContext} from "node:vm";
import {shortFlac} from "./audio-fixtures.mjs";
import {audioPlayerHtml} from "../src/audio-player.ts";

test("ChatGPT globals can mount a cold player and read private audio without standard host tool support", async()=>{
  const id="00000000-0000-4000-8000-000000000001", handlers={}, messages=[], calls=[];
  const elem=()=>({hidden:true,removeAttribute(k){delete this[k];}});
  const elements=Object.fromEntries(["#transport","#play","input","output","p","footer button","a"].map(k=>[k,elem()]));
  const bytes=shortFlac, base={prompt_id:id,index:0};
  const meta={...base,delivery:"inline",filename:"sample.flac",url:"https://split.example/view?filename=sample.flac",
    size:bytes.length,mimeType:"audio/flac",byte_offset:0,byte_length:bytes.length,next_offset:null};
  let decoded, started, closed=false;
  const parent={postMessage(m){messages.push(m);}},win={openai:{async callTool(name,args){
    calls.push({name,args});
    return {structuredContent:meta,content:[],_meta:{"split/audioPart":Buffer.from(bytes).toString("base64")}};
  }}};
  class AudioContext {
    currentTime=0;
    async resume(){}
    async decodeAudioData(bytes){decoded=new Uint8Array(bytes);return {duration:5};}
    createBufferSource(){return {connect(){},disconnect(){},start(_,at){started=at;},stop(){}};}
    async close(){closed=true;}
  }
  const script=audioPlayerHtml.match(/<script>([\s\S]*?)<\/script>/)[1];
  runInNewContext(script,{window:win,parent,document:{querySelector:k=>elements[k],body:{scrollHeight:100}},
    URL,Blob,AudioContext,atob,Uint8Array,setTimeout,clearTimeout,setInterval,clearInterval,addEventListener:(n,f)=>{handlers[n]=f;},
    ResizeObserver:class{observe(){}}});
  handlers.message({source:parent,origin:"https://host.example",data:{id:1,jsonrpc:"2.0",result:{hostCapabilities:{}}}});
  const result={structuredContent:{...base,delivery:"starting"},content:[{type:"text",text:"Starting"}]};
  handlers["openai:set_globals"]({detail:{globals:{toolResponseMetadata:{mcp_tool_result:result}}}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(elements["#transport"].hidden,false);
  assert.equal(decoded,undefined); // Decoding and sound start only on the user's click.
  await elements["#play"].onclick();
  assert.deepEqual(decoded,bytes);assert.equal(started,0);
  elements.input.value=2;elements.input.oninput();assert.equal(started,2);
  await elements["#play"].onclick();assert.equal(elements["#play"].textContent,"再生");
  assert.equal(elements.a.href,meta.url);
  handlers.message({source:parent,origin:"https://host.example",data:{id:4,jsonrpc:"2.0",method:"ui/resource-teardown"}});
  assert.equal(closed,true);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),[{name:"read_audio_part",args:{...base,byte_offset:0}}]);
  assert.ok(!messages.some(m=>m.method==="tools/call"));
});
