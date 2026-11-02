import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

import {libraryRequest} from "../../mcp/library.mjs";
import {adminTools} from "./admin-tools.ts";
import {filename, workflowInputs, production} from "./production-tools.ts";
import { readAtMost } from "./bounded-response.ts";
import { listModels, modelInputs } from "./model-inventory.ts";
import { uploadInputs, uploadMedia } from "./upload-media.ts";
import { PLAYER_MIME, PLAYER_URI, playerHtml } from "./video-player.ts";
import { audioInputs, audioSchema, getMedia, videoInputs, videoSchema } from "./media.ts";

const graph = z.record(z.string(), z.unknown());
const relative = z.string().max(512).refine(s => !s.includes("\\") && !s.startsWith("/") &&
  !s.split("/").includes("..") && !/[\x00-\x1f]/.test(s));
const failure = (text:string): CallToolResult => ({isError:true,content:[{type:"text",text}]});
type Send = (path:string, options: RequestInit) => Promise<Response>;

async function rejected(response: Response): Promise<CallToolResult> {
  const fallback = "Request rejected with HTTP 409.";
  try {
    const bytes = await readAtMost(response, 8192);
    if (!bytes) return failure(fallback);
    const reason = JSON.parse(new TextDecoder().decode(bytes))?.error;
    if (typeof reason !== "string") return failure(fallback);
    const detail = reason.replace(/[\x00-\x1f\x7f]/g, " ").trim().slice(0, 300);
    return failure(detail ? fallback + " " + detail : fallback);
  } catch {
    return failure(fallback);
  }
}

// Only a tool invocation may call send(). No polling survives this invocation.
export async function callSplit(send:Send, name:string, args:Record<string,unknown>, browserOrigin?:string, blobHosts?:string): Promise<CallToolResult> {
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
        if (name === "upload_media") return uploadMedia(send,args,fetch,blobHosts);
        if (name === "list_models") return await listModels(send,args);
        if (name === "cancel_job" || name === "get_job_progress") {
          const r = await send(name === "cancel_job" ? "/_remote_mcp/cancel" : "/_remote_mcp/progress",{method:"POST",headers:{"X-Split-Remote-Tool":"1"},
            body:JSON.stringify({prompt_id:args.prompt_id}),signal:AbortSignal.timeout(30_000)});
          if (r.status===409) return rejected(r);
          if (!r.ok && r.status!==404) return failure(name+" failed ("+r.status+"); check this prompt_id.");
          return {isError:!r.ok,content:[{type:"text",text:JSON.stringify(await r.json())}]};
        }
        if (name === "get_workflow" && !libraryRequest(args)) return failure("Invalid workflow operation or library filename.");
        if (Object.hasOwn(production,name) || Object.hasOwn(adminTools,name)) {
          const r=await send("/_remote_mcp/operations",{method:"POST",headers:{"X-Split-Remote-Tool":"1","Content-Type":"application/json"},
            body:JSON.stringify({name,arguments:args}),signal:AbortSignal.timeout(55_000)});
          const bytes=await readAtMost(r,8*1024*1024);
          if(!bytes) return failure("Response too large; narrow the request.");
          const result=JSON.parse(new TextDecoder().decode(bytes));
          return {isError:!r.ok || (Object.hasOwn(adminTools,name) && ["failed","unknown"].includes(result?.status)),content:[{type:"text",text:JSON.stringify(result)}]};
        }
        if (name === "get_video" || name === "get_audio") {
          if (!browserOrigin) return failure("Missing split browser origin.");
          return name === "get_audio"
            ? await getMedia("audio", send, browserOrigin, audioSchema.parse(args))
            : await getMedia("video", send, browserOrigin, videoSchema.parse(args));
        }
        const submit = name === "enqueue_workflow";
        const r = await send(submit ? "/prompt" : "/_remote_mcp/call",{
          method:"POST",signal:AbortSignal.timeout(50_000),
          headers: submit ? {"Idempotency-Key":"remote-mcp:"+args.request_id} : {"X-Split-Remote-Tool":"1"},
          body:JSON.stringify(submit ? {prompt:args.workflow,client_id:"remote-mcp"} : {name,arguments:args}),
        });
        if (r.status === 409) return await rejected(r);
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

export async function remoteMcp(request:Request,send:Send, browserOrigin:string, blobHosts?:string) {
  const u = new URL(request.url);
  if (request.headers.has("Origin") && request.headers.get("Origin") !== u.origin)
    return new Response("Invalid origin",{status:403});
  // Stateless HTTP: no idle SSE or client pings are forwarded to Modal.
  if (request.method !== "POST") return new Response(null,{status:405,headers:{Allow:"POST"}});
  const server = new McpServer({name:"comfyui-split",version:"1.0.0"});
  const read = {readOnlyHint:true,destructiveHint:false,openWorldHint:false};
  server.registerTool("calculate",{description:"Calculate dimensions, seeds or a batch of numeric expressions.",annotations:read,
    inputSchema:{spec:z.union([z.string().max(4096),z.array(z.string().max(1024)).max(64)]),variables:z.record(z.string(),z.number()).optional(),seed:z.number().int().optional()}},a=>callSplit(send,"calculate",a));
  server.registerTool("search_custom_nodes",{description:"Find a node pack in the public Comfy Registry or read its release details. Does not install it.",annotations:{...read,openWorldHint:true},
    inputSchema:{action:z.enum(["search","details"]),query:z.string().max(150).optional(),id:z.string().regex(/^[a-z0-9][a-z0-9_-]{0,99}$/).optional(),limit:z.number().int().min(1).max(50).optional(),page:z.number().int().positive().optional()}},a=>callSplit(send,"search_custom_nodes",a));
  server.registerTool("get_workflow",{description:"List/get/analyze/query/strip/slice saved library workflows without generating. Use analyze for sections, query for matching nodes.",annotations:read,inputSchema:workflowInputs},a=>callSplit(send,"get_workflow",a));
  server.registerTool("save_workflow",{description:"Save a NEW named workflow in the ComfyUI library. Existing names are never overwritten. Converts API graphs for the editor when possible.",annotations:{readOnlyHint:false,destructiveHint:false,openWorldHint:false},
    inputSchema:{filename,workflow:graph}},a=>callSplit(send,"save_workflow",{...a,action:"save"}));
  server.registerTool("visualize_workflow",{description:"Draw or convert a supplied graph as Mermaid or workflow DSL, without running it.",annotations:read,inputSchema:{action:z.enum(["render","render_hierarchical","mermaid","to_dsl","from_dsl"]),workflow:graph.optional(),mermaid:z.string().optional(),dsl:z.string().optional(),show_values:z.boolean().optional(),direction:z.enum(["LR","TB"]).optional(),view:z.enum(["overview","detail","list","summary"]).optional(),section:z.string().optional()}},a=>callSplit(send,"visualize_workflow",a));
  for(const [name,tool] of Object.entries({...production,...adminTools})) {
    server.registerTool(name,{description:tool.description,annotations:{readOnlyHint:!tool.write,destructiveHint:tool.write && name!=="reuse_output",openWorldHint:name==="add_model" || name==="change_node"},
      inputSchema:tool.inputs},(a:Record<string,unknown>)=>callSplit(send,name,a));
  }
  server.registerTool("upload_media",{description:"Save an attached image, audio file or video (up to 256 MiB) to split inputs. Returns the loader filename. CLI clients use resumable parts; never pass local paths.",
    annotations:{readOnlyHint:false,destructiveHint:false,idempotentHint:true,openWorldHint:true},
    _meta:{"openai/fileParams":["file"]},inputSchema:uploadInputs},a=>callSplit(send,"upload_media",a,undefined,blobHosts));
  server.registerTool("list_models",{description:"List installed ComfyUI model categories, or filenames in one category. Supports paging and substring search; reads only.",annotations:read,inputSchema:modelInputs}, a=>callSplit(send,"list_models",a));
  server.registerTool("get_job_progress",{description:"Inspect one split prompt_id: status, current node and reported units (value/max), wall and execution time, and failure details. Unreported values are null. Does not start a job or subscribe.",annotations:read,
    inputSchema:{prompt_id:z.uuid()}}, a=>callSplit(send,"get_job_progress",a));
  server.registerTool("cancel_job",{description:"Cancel exactly one queued or running split prompt_id. cancel_requested means interruption is pending; check queue status. Finished outputs are retained.",
    annotations:{readOnlyHint:false,destructiveHint:true,idempotentHint:true,openWorldHint:false},
    inputSchema:{prompt_id:z.uuid()}},a=>callSplit(send,"cancel_job",a));
  server.registerTool("create_workflow",{description:"Inspect installed nodes, create, edit or validate an API workflow. Does not generate.",
    annotations:read,inputSchema:{action:z.enum(["node_info","create","modify","validate"]),node_type:z.string().optional(),
      template:z.string().optional(),params:graph.optional(),workflow:graph.optional(),operations:z.array(graph).optional(),
      health:z.boolean().optional(),verbose:z.boolean().optional(),refresh:z.boolean().optional(),raw:z.boolean().optional()}}, a=>callSplit(send,"create_workflow",a));
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
  server.registerResource("video-player", PLAYER_URI, {mimeType:PLAYER_MIME}, async () => ({
    contents:[{uri:PLAYER_URI, mimeType:PLAYER_MIME, text:playerHtml}],
  }));
  server.registerTool("get_video",{title:"Retrieve a generated video",
    description:"Retrieve the MP4 from a completed split prompt_id. Returns video bytes or an authenticated browser link. For larger files request chunks using byte_offset and next_offset. Does not generate.",
    _meta:{ui:{resourceUri:PLAYER_URI}, "openai/outputTemplate":PLAYER_URI},
    annotations:read,inputSchema:videoInputs}, a=>callSplit(send,"get_video",a,browserOrigin));
  server.registerTool("get_audio",{title:"Retrieve generated audio",
    description:"Read a saved WAV, FLAC, MP3, Ogg or Opus audio file from a completed split prompt_id. Small files return MCP audio; for larger files use the authenticated link or byte_offset and next_offset. Does not generate or extract sound from video.",
    annotations:read,inputSchema:audioInputs}, a=>callSplit(send,"get_audio",a,browserOrigin));
  const transport = new WebStandardStreamableHTTPServerTransport({sessionIdGenerator:undefined,enableJsonResponse:true});
  await server.connect(transport);
  try { return await transport.handleRequest(request); } finally { await server.close(); }
}
