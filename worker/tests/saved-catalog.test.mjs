import assert from "node:assert/strict";
import {test} from "node:test";
import {callSplit} from "../src/remote-mcp.ts";

const id="fe08cba3-93d1-439f-b5c4-ccb6d9be3421", origin="https://split.example.com";
const graph={"1":{class_type:"EmptyImage",inputs:{width:64,height:64}}};
test("saved models, workflow files, histories and submitted graphs never probe ComfyUI",async()=>{
  const split=()=>{throw new Error("Must not start ComfyUI");};
  const paths=[];
  const media=async(path,init)=>{
    paths.push(path);assert.equal(init.method,"GET");
    if(path.includes("action=get"))return Response.json({conversion_required:false,workflow:graph});
    return Response.json({source:"storage",items:[],workflows:["日本語/編集.json"],models:["model.safetensors"]});
  };
  for(const [name,args] of [["list_models",{}],["list_models",{category:"checkpoints",query:"model",limit:1}],
    ["get_workflow",{action:"list"}],["get_workflow",{action:"get",filename:"日本語/編集.json",format:"ui"}],
    ["get_workflow",{action:"get",filename:"API.json"}],["get_history",{action:"list"}],
    ["get_history",{action:"diagnose",prompt_id:id}],["get_job_workflow",{prompt_id:id}]]) {
    const r=await callSplit(split,name,args,origin,undefined,media);assert.ok(!r.isError,JSON.stringify(r));
    if(name==="get_workflow" && args.action==="get")assert.deepEqual(JSON.parse(r.content[0].text),graph);
  }
  assert.ok(paths.every(p=>p.startsWith("/catalog/")));
  assert.ok(paths.some(p=>decodeURIComponent(p).includes("日本語/編集.json")));
});
test("only an explicit UI conversion marker or runtime inventory may use ComfyUI",async()=>{
  const paths=[];
  const split=async(path)=>{
    paths.push(path);
    if(path==="/split/startup")return Response.json({ready:true});
    if(path==="/models")return Response.json(["checkpoints"]);
    return Response.json({content:[{type:"text",text:JSON.stringify(graph)},{type:"text",text:"conversion warning"}]});
  };
  const converted=await callSplit(split,"get_workflow",{action:"get",filename:"編集.json"},origin,undefined,
    async()=>Response.json({conversion_required:true}));
  assert.equal(converted.content[1].text,"conversion warning");
  assert.deepEqual(paths,["/split/startup","/_remote_mcp/call"]);
  paths.length=0;
  const runtime=await callSplit(split,"list_models",{source:"runtime"},origin,undefined,()=>{throw Error("Wrong route");});
  assert.equal(JSON.parse(runtime.content[0].text).source,"runtime");
  assert.deepEqual(paths,["/split/startup","/models"]);
});
test("missing, malformed and failed saved reads fail closed without waking ComfyUI",async()=>{
  const split=()=>{throw Error("Must not start ComfyUI");};
  for(const response of [()=>new Response("missing",{status:404}),()=>new Response("broken",{status:503}),
    ()=>new Response("not JSON"),()=>Response.json({workflow:graph}),()=>{throw Error("offline");}]) {
    const r=await callSplit(split,"get_workflow",{action:"get",filename:"x.json"},origin,undefined,response);
    assert.equal(r.isError,true);
  }
  for(const [name,args] of [["list_models",{category:"../private"}],["get_workflow",{action:"get",filename:"../x.json"}],
    ["get_history",{action:"list",prompt_id:"invalid"}],["get_job_workflow",{prompt_id:id,filename:"secret"}]]) {
    const r=await callSplit(split,name,args,origin,undefined,()=>{throw Error("Must not read storage");});
    assert.equal(r.isError,true);
  }
});
