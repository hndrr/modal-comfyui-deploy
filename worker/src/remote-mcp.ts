import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

import { readAtMost } from "./bounded-response.ts";
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
export async function callSplit(send:Send, name:string, args:Record<string,unknown>, browserOrigin?:string): Promise<CallToolResult> {
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

export async function remoteMcp(request:Request,send:Send, browserOrigin:string) {
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
