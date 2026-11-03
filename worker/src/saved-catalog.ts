import type {CallToolResult} from "@modelcontextprotocol/sdk/types.js";
import {z} from "zod";
import {libraryRequest} from "../../mcp/library.mjs";
import {workflowInputs} from "./production-tools.ts";
import {modelInputs} from "./model-inventory.ts";
import {readAtMost} from "./bounded-response.ts";

export function savedCatalogRequest(name:string,args:Record<string,unknown>):boolean {
  return (name==="list_models" && args.source!=="runtime") || name==="get_history" || name==="get_job_workflow" ||
    (name==="get_workflow" && (args.action==="list" || args.action==="get"));
}
type Send=(path:string,init:RequestInit)=>Promise<Response>;
const fail=(text:string):CallToolResult=>({isError:true,content:[{type:"text",text}]});

// null is ONLY an explicit UI -> API conversion requiring live node definitions.
// Missing files, storage failures and malformed replies never wake ComfyUI.
export async function callSavedCatalog(send:Send,name:string,args:Record<string,unknown>):Promise<CallToolResult|null> {
  try {
    let route:string, values:Record<string,unknown>;
    if(name==="list_models") {
      const {source,...rest}=z.object(modelInputs).strict().parse(args);
      if(source==="runtime")throw new Error("Runtime inventory requires ComfyUI");
      route="models";values=rest;
    } else if(name==="get_workflow") {
      values=z.object(workflowInputs).strict().parse(args);
      if(!libraryRequest(values))throw new Error("Invalid workflow library request");
      route="workflows";
    } else if(name==="get_history") {
      values=z.object({action:z.enum(["list","diagnose"]),prompt_id:z.uuid().optional(),
        limit:z.number().int().min(1).max(100).optional()}).strict().parse(args);
      route="history";
    } else if(name==="get_job_workflow") {
      values=z.object({prompt_id:z.uuid()}).strict().parse(args);route="job-workflow";
    } else throw new Error("Unsupported saved catalog tool");
    const q=new URLSearchParams(Object.entries(values).filter(([,v])=>v!==undefined).map(([k,v])=>[k,String(v)]));
    const r=await send("/catalog/"+route+"?"+q,{method:"GET",signal:AbortSignal.timeout(50_000)});
    const bytes=await readAtMost(r,8*1024*1024);
    if(!bytes)return fail("Saved response exceeds the read limit; narrow the request.");
    if(!r.ok)return fail("Saved catalog read failed (HTTP "+r.status+"). No ComfyUI process was started; retry the read.");
    const data=JSON.parse(new TextDecoder().decode(bytes));
    if(name==="get_workflow" && args.action==="get") {
      if(data.conversion_required===true && (args.format??"api")==="api")return null;
      if(data.conversion_required!==false || !data.workflow || typeof data.workflow!=="object" || Array.isArray(data.workflow))
        throw new Error("Invalid saved workflow response");
      return {content:[{type:"text",text:JSON.stringify(data.workflow)}]};
    }
    return {content:[{type:"text",text:JSON.stringify(data)}]};
  } catch {return fail("Saved catalog is unavailable or the request is invalid. No ComfyUI process was started.");}
}
