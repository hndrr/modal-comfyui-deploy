import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { readAtMost } from "./bounded-response.ts";

export const modelInputs = {
  source:z.enum(["storage","runtime"]).optional().describe("Default storage lists saved files without starting ComfyUI. Runtime starts ComfyUI to list its registered categories and recognized models."),
  category:z.string().regex(/^[a-zA-Z0-9_-]{1,80}$/).optional().describe("Omit to list installed model categories."),
  query:z.string().max(160).optional().describe("Case-insensitive filename or category substring."),
  offset:z.number().int().min(0).optional(), limit:z.number().int().min(1).max(200).optional(),
};
const schema = z.object(modelInputs).strict();
export async function listModels(send:(path:string,init:RequestInit)=>Promise<Response>, args:unknown):Promise<CallToolResult> {
  const {category,query,offset=0,limit=100} = schema.parse(args);
  const r = await send(category ? "/models/"+category : "/models",{method:"GET",signal:AbortSignal.timeout(30_000)});
  if (r.ok) {
    const bytes = await readAtMost(r,4*1024*1024);
    const rows = bytes && JSON.parse(new TextDecoder().decode(bytes));
    if (Array.isArray(rows) && rows.every(x=>typeof x==="string")) {
      const filtered:string[] = rows.filter(x=>!query || x.toLowerCase().includes(query.toLowerCase())).sort();
      return {content:[{type:"text",text:JSON.stringify({source:"runtime",category:category??null,[category?"models":"categories"]:filtered.slice(offset,offset+limit),
        total:filtered.length,next_offset:offset+limit<filtered.length?offset+limit:null})}]};
    }
  } else { await r.body?.cancel(); }
  return {isError:true,content:[{type:"text",text:"Could not read the model inventory. Check the category or retry."}]};
}
