import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

const graph = z.record(z.string(), z.unknown());
const relative = z.string().max(512).refine(s => !s.includes("\\") && !s.startsWith("/") &&
  !s.split("/").includes("..") && !/[\x00-\x1f]/.test(s));
const failure = (text:string): CallToolResult => ({isError:true,content:[{type:"text",text}]});
type Send = (path:string, options: RequestInit) => Promise<Response>;

// Only a tool invocation may call send(). No polling survives this invocation.
export async function callSplit(send:Send, name:string, args:Record<string,unknown>): Promise<CallToolResult> {
  const deadline = Date.now() + 12_000;
  try {
    for (;;) {
      const left = deadline - Date.now();
      if (left <= 0) break;
      let res:Response;
      try { res = await send("/split/startup",{signal:AbortSignal.timeout(left)}); }
      catch (e) { if (Date.now() >= deadline || (e as Error).name === "TimeoutError") break; throw e; }
      if (!res.ok) return failure("CPU readiness check failed ("+res.status+"). No tool was executed.");
      const state = await res.json<{ready:boolean;failed:boolean}>();
      if (state.failed) return failure("Split CPU initialization failed. No tool was executed.");
      if (state.ready) {
        const submit = name === "enqueue_workflow";
        const r = await send(submit ? "/prompt" : "/_remote_mcp/call",{
          method:"POST",signal:AbortSignal.timeout(50_000),
          headers: submit ? {"Idempotency-Key":"remote-mcp:"+args.request_id} : {"X-Split-Remote-Tool":"1"},
          body:JSON.stringify(submit ? {prompt:args.workflow,client_id:"remote-mcp"} : {name,arguments:args}),
        });
        if (r.status === 409) return failure("Conflict: a different workflow used this request_id, or split mode is unavailable.");
        if (!r.ok) return failure("Split tool returned HTTP "+r.status+
          (submit ? ". If retrying this submission, reuse the same request_id and workflow." : "."));
        if (submit) {
          const job = await r.json<{prompt_id:string;number:number}>();
          if (!job.prompt_id) throw new Error("Missing receipt");
          return {content:[{type:"text",text:JSON.stringify({request_id:args.request_id,prompt_id:job.prompt_id,number:job.number})}]};
        }
        return await r.json<CallToolResult>();
      }
      await new Promise(resolve=>setTimeout(resolve,Math.min(1500,Math.max(0,deadline-Date.now()))));
    }
    return failure("Split CPU is starting. Retry after 2 seconds; keep the same request_id. No tool was executed.");
  } catch {
    return failure(name==="enqueue_workflow" ?
      "No receipt available. Retry only with the SAME request_id and unchanged workflow." : "Split CPU request failed; retry the read.");
  }
}

export async function remoteMcp(request:Request,send:Send) {
  const u = new URL(request.url);
  if (request.headers.has("Origin") && request.headers.get("Origin") !== u.origin)
    return new Response("Invalid origin",{status:403});
  // Stateless HTTP: no idle SSE or client pings are forwarded to Modal.
  if (request.method !== "POST") return new Response(null,{status:405,headers:{Allow:"POST"}});
  const server = new McpServer({name:"comfyui-split",version:"1.0.0"});
  const read = {readOnlyHint:true,destructiveHint:false,openWorldHint:false};
  server.registerTool("create_workflow",{description:"Inspect installed nodes, create, edit or validate an API workflow. Does not generate.",
    annotations:read,inputSchema:{action:z.enum(["node_info","create","modify","validate"]),node_type:z.string().optional(),
      template:z.string().optional(),params:graph.optional(),workflow:graph.optional(),operations:z.array(graph).optional(),
      refresh:z.boolean().optional(),raw:z.boolean().optional()}}, a=>callSplit(send,"create_workflow",a));
  server.registerTool("enqueue_workflow",{description:"Generate once. Choose a unique request_id for each new render; preserve it and the identical graph across retries, disconnects and agents. Returns prompt_id.",
    annotations:{readOnlyHint:false,destructiveHint:false,idempotentHint:true,openWorldHint:false},
    inputSchema:{request_id:z.string().regex(/^[a-zA-Z0-9_-]{1,96}$/),workflow:graph}},a=>callSplit(send,"enqueue_workflow",a));
  server.registerTool("queue",{description:"List jobs or check prompt_id status.",annotations:read,
    inputSchema:{action:z.enum(["list","status"]),prompt_id:z.string().optional()}},a=>callSplit(send,"queue",a));
  server.registerTool("get_history",{description:"Get job history, including the output filename and subfolder.",annotations:read,
    inputSchema:{action:z.enum(["list","diagnose"]),prompt_id:z.string().optional(),limit:z.number().int().min(1).max(100).optional()}},
    a=>callSplit(send,"get_history",a));
  server.registerTool("get_image",{description:"Return a generated output image inline using its history filename.",annotations:read,
    inputSchema:{filename:relative,subfolder:relative.optional()}},a=>callSplit(send,"get_image",{...a,action:"get"}));
  const transport = new WebStandardStreamableHTTPServerTransport({sessionIdGenerator:undefined,enableJsonResponse:true});
  await server.connect(transport);
  try { return await transport.handleRequest(request); } finally { await server.close(); }
}
