import {receiptLink} from "./job-review.ts";
import {callSavedMedia, imageOutput, savedMediaTools} from "./saved-media.ts";
import {callSavedCatalog, savedCatalogRequest} from "./saved-catalog.ts";
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
import { PLAYER_MIME, PLAYER_URI, PREVIOUS_PLAYER_URIS, playerHtml } from "./video-player.ts";
import { IMAGE_VIEWER_URI, imageViewerHtml } from "./image-viewer.ts";
import { AUDIO_PLAYER_URI, PREVIOUS_AUDIO_PLAYER_URIS, audioPlayerHtml } from "./audio-player.ts";
import { AUDIO_PART_BYTES, audioPartInputs, audioInputs, audioOutput, audioSchema, getMedia, VIDEO_PART_BYTES, videoPartInputs, videoInputs, videoOutput, videoSchema } from "./media.ts";

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
export async function callSplit(send:Send, name:string, args:Record<string,unknown>, browserOrigin?:string, blobHosts?:string, mediaSend?:Send): Promise<CallToolResult> {
  if (mediaSend && savedCatalogRequest(name,args)) {
    const result=await callSavedCatalog(mediaSend,name,args);
    if(result!==null)return result;
  }
  if (mediaSend && savedMediaTools.has(name))
    return callSavedMedia(mediaSend,name,args,browserOrigin || "https://split.invalid");
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
        if (name === "get_image") {
          if (!browserOrigin) return failure("Missing split browser origin.");
          return callSavedMedia(send,name,args,browserOrigin);
        }
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
        if (name === "get_video" || name === "get_audio" || name === "read_audio_part" || name === "read_video_part") {
          if (!browserOrigin) return failure("Missing split browser origin.");
          if (name === "read_audio_part") return await getMedia("audio", send, browserOrigin,
            {...z.object(audioPartInputs).parse(args), chunk_bytes:AUDIO_PART_BYTES}, true);
          if (name === "read_video_part") return await getMedia("video", send, browserOrigin,
            {...z.object(videoPartInputs).parse(args), chunk_bytes:VIDEO_PART_BYTES}, true);
          return name === "get_audio"
            ? await getMedia("audio", send, browserOrigin, audioSchema.parse(args))
            : await getMedia("video", send, browserOrigin, videoSchema.parse(args));
        }
        const submit = name === "enqueue_workflow";
        const r = await send(submit ? "/_remote_mcp/submit" : "/_remote_mcp/call",{
          method:"POST",signal:AbortSignal.timeout(50_000),
          headers: submit ? {"X-Split-Remote-Tool":"1","Idempotency-Key":"remote-mcp:"+args.request_id} : {"X-Split-Remote-Tool":"1"},
          body:JSON.stringify(submit ? {prompt:args.workflow,client_id:"remote-mcp",gpu:args.gpu} : {name,arguments:args}),
        });
        if (r.status === 409) return await rejected(r);
        if (!r.ok) return failure("Split tool returned HTTP "+r.status+
          (submit ? ". If retrying this submission, reuse the same request_id and workflow." : "."));
        if (submit) {
          if (r.status===202) {
            const pending=await r.json<{status:string;approval_path:string;expires_at:number;gpu?:string;gpu_reason?:string}>();
            return {content:[{type:"text",text:JSON.stringify({status:"approval_required",request_id:args.request_id,
              approval_url:receiptLink(pending,browserOrigin),expires_at:pending.expires_at,gpu:pending.gpu,gpu_reason:pending.gpu_reason,
              instructions:"Give this link to the owner. Do not open or approve it on their behalf. No job has started."})}]};
          }
          const job = await r.json<{prompt_id:string;number:number;gpu?:string;gpu_reason?:string}>();
          if (!job.prompt_id) throw new Error("Missing receipt");
          return {content:[{type:"text",text:JSON.stringify({request_id:args.request_id,prompt_id:job.prompt_id,number:job.number,gpu:job.gpu,gpu_reason:job.gpu_reason})}]};
        }
        return await r.json<CallToolResult>();
      }
      if ((name==="get_audio" || name==="get_video") && args.byte_offset===undefined && args.chunk_bytes===undefined) break;
      await new Promise(resolve=>setTimeout(resolve,Math.min(1500,Math.max(0,deadline-Date.now()))));
    }
    if ((name==="get_audio" || name==="get_video") && args.byte_offset===undefined && args.chunk_bytes===undefined) {
      const {prompt_id,index=0}=audioSchema.parse(args);
      return {structuredContent:{delivery:"starting",prompt_id,index},content:[{type:"text",
        text:"The media player is waiting for split to start. No generation was submitted. Clients without a player can retry " + name + "."}]};
    }
    const waiting=failure("Split CPU is starting. Retry after 2 seconds; keep the same request_id. No tool was executed.");
    return name==="read_audio_part" || name==="read_video_part" ? {...waiting,_meta:{"split/starting":true}} : waiting;
  } catch {
    return failure(name==="enqueue_workflow" ?
      "No receipt available. Retry only with the SAME request_id and unchanged workflow." : "Split CPU request failed; retry the read.");
  }
}

export async function remoteMcp(request:Request,send:Send, browserOrigin:string, blobHosts?:string, mediaSend?:Send) {
  const u = new URL(request.url);
  if (request.headers.has("Origin") && request.headers.get("Origin") !== u.origin)
    return new Response("Invalid origin",{status:403});
  // Stateless HTTP: no idle SSE or client pings are forwarded to Modal.
  if (request.method !== "POST") return new Response(null,{status:405,headers:{Allow:"POST"}});
  const server = new McpServer({name:"comfyui-split",version:"1.0.0"});
  const call=(name:string,args:Record<string,unknown>,origin?:string,hosts?:string)=>
    callSplit(send,name,args,origin || browserOrigin,hosts,mediaSend);
  const read = {readOnlyHint:true,destructiveHint:false,openWorldHint:false};
  server.registerTool("calculate",{description:"Calculate dimensions, seeds or a batch of numeric expressions.",annotations:read,
    inputSchema:{spec:z.union([z.string().max(4096),z.array(z.string().max(1024)).max(64)]),variables:z.record(z.string(),z.number()).optional(),seed:z.number().int().optional()}},a=>call("calculate",a));
  server.registerTool("search_custom_nodes",{description:"Find a node pack in the public Comfy Registry or read its release details. Does not install it.",annotations:{...read,openWorldHint:true},
    inputSchema:{action:z.enum(["search","details"]),query:z.string().max(150).optional(),id:z.string().regex(/^[a-z0-9][a-z0-9_-]{0,99}$/).optional(),limit:z.number().int().min(1).max(50).optional(),page:z.number().int().positive().optional()}},a=>call("search_custom_nodes",a));
  server.registerTool("get_workflow",{description:"List/get/analyze/query/strip/slice saved library workflows without generating. List and get(format:ui) read storage only. Default get returns API JSON; converting a UI graph starts ComfyUI for current node definitions. Analyze/query/strip/slice use ComfyUI.",annotations:read,inputSchema:workflowInputs},a=>call("get_workflow",a));
  server.registerTool("save_workflow",{description:"Save a NEW named workflow in the ComfyUI library. Existing names are never overwritten. Converts API graphs for the editor when possible.",annotations:{readOnlyHint:false,destructiveHint:false,openWorldHint:false},
    inputSchema:{filename,workflow:graph}},a=>call("save_workflow",{...a,action:"save"}));
  server.registerTool("visualize_workflow",{description:"Use render to turn workflow JSON into Mermaid; mermaid parses flowchart text back to JSON. Also converts workflow DSL. Does not generate.",annotations:read,inputSchema:{action:z.enum(["render","render_hierarchical","mermaid","to_dsl","from_dsl"]),workflow:graph.optional(),mermaid:z.string().optional(),dsl:z.string().optional(),show_values:z.boolean().optional(),direction:z.enum(["LR","TB"]).optional(),view:z.enum(["overview","detail","list","summary"]).optional(),section:z.string().optional()}},a=>call("visualize_workflow",a));
  for(const [name,tool] of Object.entries({...production,...adminTools})) {
    server.registerTool(name,{description:tool.description,annotations:{readOnlyHint:!tool.write,destructiveHint:tool.write && name!=="reuse_output",openWorldHint:name==="add_model" || name==="change_node"},
      inputSchema:tool.inputs},(a:Record<string,unknown>)=>call(name,a));
  }
  server.registerTool("upload_media",{description:"Save an attached image, audio file or video (up to 256 MiB) to split inputs. Returns the loader filename. CLI clients use resumable parts; never pass local paths.",
    annotations:{readOnlyHint:false,destructiveHint:false,idempotentHint:true,openWorldHint:true},
    _meta:{"openai/fileParams":["file"]},inputSchema:uploadInputs},a=>call("upload_media",a,undefined,blobHosts));
  server.registerTool("list_models",{description:"List saved model categories or files with sizes and modification times, without starting ComfyUI. Storage files are not guaranteed to be recognized by ComfyUI. Use source:runtime to check its actual registered models. Supports paging and substring search.",annotations:read,inputSchema:modelInputs}, a=>call("list_models",a));
  server.registerTool("get_job_progress",{description:"Inspect one split prompt_id: status, selected CPU/GPU and reason, current node and reported units (value/max), wall and execution time, and failure details. Unreported values are null. Does not start a job or subscribe.",annotations:read,
    inputSchema:{prompt_id:z.uuid()}}, a=>call("get_job_progress",a));
  server.registerTool("cancel_job",{description:"Cancel exactly one queued or running split prompt_id. cancel_requested means interruption is pending; check queue status. Finished outputs are retained.",
    annotations:{readOnlyHint:false,destructiveHint:true,idempotentHint:true,openWorldHint:false},
    inputSchema:{prompt_id:z.uuid()}},a=>call("cancel_job",a));
  server.registerTool("create_workflow",{description:"Inspect installed nodes, create, edit or validate an API workflow. Does not generate.",
    annotations:read,inputSchema:{action:z.enum(["node_info","create","modify","validate"]),node_type:z.string().optional(),
      template:z.string().optional(),params:graph.optional(),workflow:graph.optional(),operations:z.array(graph).optional(),
      health:z.boolean().optional(),verbose:z.boolean().optional(),refresh:z.boolean().optional(),raw:z.boolean().optional()}}, a=>call("create_workflow",a));
  server.registerTool("enqueue_workflow",{description:"Generate once. Choose a unique request_id for each new render; preserve it, the CPU/GPU choice and the identical graph across retries, disconnects and agents. Local generation returns prompt_id immediately. External providers return an owner review URL; only the owner may approve sending the displayed data and incurring charges.",
    annotations:{readOnlyHint:false,destructiveHint:false,idempotentHint:true,openWorldHint:true},
    inputSchema:{request_id:z.string().regex(/^[a-zA-Z0-9_-]{1,96}$/),workflow:graph,
      gpu:z.enum(["auto","auto-speed","CPU","L4","L40S","RTX-PRO-6000"]).optional().describe("CPU-supported API/media-I/O-only workflows use CPU ahead of auto (default, cost priority) or a manual GPU choice. With auto-speed and no external-provider approval wait, API/media-I/O-only workflows reuse an already-running GPU even if CPU-supported; otherwise they use CPU if supported, or L4 for recognized API/media-I/O-only workflows. Approval-waiting jobs pin this graph-based CPU/L4 choice without checking warm GPUs. Local inference uses RTX-PRO-6000 for auto-speed. Other workflows retain the existing GPU selection; L4/L40S/RTX-PRO-6000 sets a manual GPU choice. Use CPU only when the user explicitly asks to try CPU: it bypasses CPU eligibility, never MCP node restrictions or external-provider approval. CPU failures are not retried on GPU and do not register nodes as CPU-supported. Preserve the same choice on retries.")}},a=>call("enqueue_workflow",a,browserOrigin));
  server.registerTool("queue",{description:"List jobs or check prompt_id status.",annotations:read,
    inputSchema:{action:z.enum(["list","status"]),prompt_id:z.string().optional()}},a=>call("queue",a));
  server.registerTool("get_history",{description:"Read saved job history, output filenames, errors and timing without starting ComfyUI. diagnose prefers a failed job in the recent history window. Saved records are not live progress; use get_job_progress for that.",annotations:read,
    inputSchema:{action:z.enum(["list","diagnose"]),prompt_id:z.string().optional(),limit:z.number().int().min(1).max(100).optional()}},
    a=>call("get_history",a));
  server.registerResource("image-viewer",IMAGE_VIEWER_URI,{mimeType:PLAYER_MIME},async()=>({
    contents:[{uri:IMAGE_VIEWER_URI,mimeType:PLAYER_MIME,text:imageViewerHtml}],
  }));
  server.registerTool("get_image",{title:"View a generated image",
    description:"View a saved PNG, JPEG or WebP using its history filename. Shows an image viewer and always returns an authenticated link to open or save the image. Include that link in your reply; do not claim the image is displayed from tool success alone. Does not generate.",annotations:read,
    _meta:{ui:{resourceUri:IMAGE_VIEWER_URI},"openai/outputTemplate":IMAGE_VIEWER_URI},
    inputSchema:{filename:relative,subfolder:relative.optional()},outputSchema:imageOutput},a=>call("get_image",a));
  const videoCsp={ui:{csp:{connectDomains:[],resourceDomains:[browserOrigin]}},
    "openai/widgetCSP":{connect_domains:[],resource_domains:[browserOrigin]}};
  for (const [i,uri] of [PLAYER_URI,...PREVIOUS_PLAYER_URIS].entries())
    server.registerResource(i===0?"video-player":"previous-video-player-"+i,uri,{mimeType:PLAYER_MIME},async()=>({
      contents:[{uri,mimeType:PLAYER_MIME,text:playerHtml(browserOrigin),_meta:videoCsp}],
    }));
  server.registerTool("get_video",{title:"Show the video player",
    description:"Show the inline player for a saved MP4. Call with only prompt_id and optional index for playback, without binary chunks. The player streams authenticated HTTPS first and uses private chunks only if needed. Prefer the player; opening the authenticated link is only a fallback. Do not claim playback is visible based only on tool success. For CLI binary download explicitly set byte_offset:0 and follow next_offset. Does not generate.",
    _meta:{ui:{resourceUri:PLAYER_URI}, "openai/outputTemplate":PLAYER_URI,"openai/widgetAccessible":true},
    annotations:read,inputSchema:videoInputs,outputSchema:videoOutput}, a=>call("get_video",a,browserOrigin));
  server.registerTool("read_video_part",{description:"Read the next verified chunk for the video player.",
    _meta:{ui:{visibility:["app"]},"openai/visibility":"private","openai/widgetAccessible":true},
    annotations:read,inputSchema:videoPartInputs,outputSchema:videoOutput}, a=>call("read_video_part",a,browserOrigin));
  server.registerTool("get_audio",{title:"Retrieve generated audio",
    description:"Play saved audio. In chat, call with only prompt_id and optional index: show the player and include the returned authenticated link as a fallback. For a binary download in a non-UI client, explicitly set byte_offset:0 and follow next_offset. Does not generate.",
    _meta:{ui:{resourceUri:AUDIO_PLAYER_URI}, "openai/outputTemplate":AUDIO_PLAYER_URI,"openai/widgetAccessible":true},
    annotations:read,inputSchema:audioInputs,outputSchema:audioOutput}, a=>call("get_audio",a,browserOrigin));
  // Clients with cached descriptors can still read the current player at their old URI.
  for (const [index,uri] of [AUDIO_PLAYER_URI,...PREVIOUS_AUDIO_PLAYER_URIS].entries())
    server.registerResource(index===0?"audio-player":"previous-audio-player-"+index,uri,{mimeType:PLAYER_MIME},async()=>({
      contents:[{uri,mimeType:PLAYER_MIME,text:audioPlayerHtml}],
    }));
  server.registerTool("read_audio_part",{description:"Read the next verified chunk for the audio player.",
    _meta:{ui:{visibility:["app"]},"openai/visibility":"private","openai/widgetAccessible":true},
    annotations:read,inputSchema:audioPartInputs,outputSchema:audioOutput}, a=>call("read_audio_part",a,browserOrigin));
  const transport = new WebStandardStreamableHTTPServerTransport({sessionIdGenerator:undefined,enableJsonResponse:true});
  await server.connect(transport);
  try { return await transport.handleRequest(request); } finally { await server.close(); }
}
