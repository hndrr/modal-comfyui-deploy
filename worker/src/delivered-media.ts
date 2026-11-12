import type {CallToolResult} from "@modelcontextprotocol/sdk/types.js";
import {z} from "zod";
import {AUDIO_PART_BYTES, VIDEO_PART_BYTES, audioSchema, videoSchema, base64, outputsOfKind} from "./media.ts";
import {THREE_PART_BYTES} from "./three-media.ts";
import {readAtMost} from "./bounded-response.ts";
import {MediaDelivery, DeliveryError, deliveryRefSchema, outputFileSchema, outputPath, mediaKind,
  type MediaSend, type OutputFile, type DeliveryRef, type DeliveryState, type SourceMedia} from "./media-delivery.ts";

export const deliveredTools=new Set(["get_image","get_audio","get_video","get_3d","read_audio_part","read_video_part","read_3d_part"]);
const problem = (e:unknown) => e instanceof DeliveryError ? e : new DeliveryError("delivery_unavailable",60);
const failure = (e:unknown):CallToolResult => {
  const p=problem(e);
  return {isError:true,content:[{type:"text",text:`Media delivery: ${p.code}. Retry retrieval; no generation was submitted.`}],
    ...(p.retry ? {_meta:{"split/deliveryPending":true,retry_after_seconds:p.retry}} : {})};
};
function summary(state:DeliveryState) {
  if(state.status==="ready") return {delivery_status:"ready",url:state.url,media_id:state.media_id,
    mimeType:state.mime_type,size:state.size_bytes,etag:state.etag,expires_at:state.expires_at};
  if(state.status==="pending") return {delivery_status:"pending",retry_after_seconds:state.retry_after_seconds};
  return {delivery_status:"unavailable",delivery_reason:state.status};
}

// Existing MCP tools keep their output shapes. Only the source of the bytes changes.
export async function callDeliveredMedia(adapter:MediaDelivery,send:MediaSend,name:string,args:Record<string,unknown>,
  fallback:()=>Promise<CallToolResult>):Promise<CallToolResult> {
  const kind=name.includes("3d") ? "3d" : name.includes("audio") ? "audio" : name.includes("video") ? "video" : "image";
  const part=name.startsWith("read_");
  if(part && args.delivery_ref===undefined) return fallback(); // Previously opened legacy widgets.
  let file:OutputFile, ref:DeliveryRef, count:number|undefined;
  let prompt_id:string|undefined, index:number|undefined;
  try {
    if(kind==="audio" || kind==="video") {
      const parsed=(kind==="audio" ? audioSchema : videoSchema).parse(args);
      prompt_id=parsed.prompt_id.toLowerCase();index=parsed.index ?? 0;
    }
    if(args.delivery_ref!==undefined) {
      ref=deliveryRefSchema.parse(args.delivery_ref);file={filename:ref.filename,subfolder:ref.subfolder};
      if(mediaKind(ref.mime_type)!==kind || (kind==="3d" &&
        (args.filename!==ref.filename || (args.subfolder || "")!==ref.subfolder)))
        throw new DeliveryError("invalid_delivery_reference");
    } else {
      if((Number(args.byte_offset) || 0)>0) return failure(new DeliveryError("delivery_reference_required"));
      if(kind==="audio" || kind==="video") {
        const history=await send("/history/"+prompt_id,{method:"GET",signal:AbortSignal.timeout(50_000)});
        if(!history.ok) {await history.body?.cancel();throw new DeliveryError("source_history_unavailable");}
        const bytes=await readAtMost(history,8*1024*1024);
        if(!bytes) throw new DeliveryError("source_history_too_large");
        const job=JSON.parse(new TextDecoder().decode(bytes))?.[prompt_id!];
        if(job?.status?.completed!==true || job?.status?.status_str!=="success") throw new DeliveryError("source_job_not_completed");
        const files=outputsOfKind(job.outputs,kind);count=files.length;
        if(!files[index!]) throw new DeliveryError("output_not_found");
        file=outputFileSchema.parse(files[index!]);
      } else {
        file=outputFileSchema.parse(args);
        const allowed=kind==="3d" ? /\.(glb|spz)$/i : /\.(png|jpe?g|webp)$/i;
        if(!allowed.test(file.filename)) throw new DeliveryError("unsupported_media");
      }
      const state=await adapter.prepare(send,file);
      if(state.status!=="ready") return failure(new DeliveryError("delivery_pending",state.status==="pending" ? state.retry_after_seconds : 60));
      ref={...file,media_id:state.media_id,etag:state.etag,mime_type:state.mime_type,size_bytes:state.size_bytes};
      if(mediaKind(ref.mime_type)!==kind) throw new DeliveryError("invalid_service_media");
    }
  } catch(e) {
    if(args.delivery_ref!==undefined || e instanceof z.ZodError) return failure(e);
    const p=problem(e),result=await fallback();
    return {...result,...(result.structuredContent ? {structuredContent:{...result.structuredContent,
      delivery_status:"unavailable",delivery_reason:p.code}} : {}),
      content:[...result.content,{type:"text",text:`External delivery unavailable (${p.code}); using the existing authenticated output route.`}]};
  }
  const metadata={...file,...(prompt_id ? {prompt_id,index} : {}),
    ...(count ? {[kind+"_count"]:count} : {}),mimeType:ref.mime_type,size:ref.size_bytes,
    url:adapter.url(ref.media_id),etag:ref.etag,delivery_status:"ready",delivery_ref:ref};
  const format=kind==="3d" ? ref.mime_type==="model/gltf-binary" ? "glb" : "spz" : undefined;
  try {
    if(kind==="image") {
      const inline=ref.size_bytes<=8*1024*1024 && ["image/png","image/jpeg","image/webp"].includes(ref.mime_type);
      const content:CallToolResult["content"]=[{type:"text",text:`${file.filename} — open or save after website login: ${metadata.url}`}];
      if(inline) content.unshift({type:"image",mimeType:ref.mime_type,data:base64(await adapter.read(ref,0,ref.size_bytes))});
      return {structuredContent:{...metadata,delivery:inline ? "inline" : "link"},content};
    }
    const chunks=part || args.byte_offset!==undefined || args.chunk_bytes!==undefined;
    if(!chunks) return {structuredContent:{...metadata,...(format ? {format} : {}),delivery:kind==="3d" ? "viewer" : "player"},
      content:[{type:"text",text:`${file.filename} — use the inline ${kind} viewer. Retrieval alone does not confirm playback. Open or save after website login: ${metadata.url}`}]};
    const offset=z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER-4*1024*1024).parse(args.byte_offset ?? 0);
    const length=part ? kind==="3d" ? THREE_PART_BYTES : kind==="audio" ? AUDIO_PART_BYTES : VIDEO_PART_BYTES :
      z.number().int().min(1).max(4*1024*1024).parse(args.chunk_bytes ?? 4*1024*1024);
    if(offset>=ref.size_bytes) throw new DeliveryError("range_out_of_bounds");
    const bytes=await adapter.read(ref,offset,length),blob=base64(bytes),next=offset+bytes.length;
    const complete=offset===0 && next===ref.size_bytes;
    const info={...metadata,...(format ? {format} : {}),delivery:part ? "chunk" : complete ? "inline" : "chunk",
      byte_offset:offset,byte_length:bytes.length,next_offset:next<ref.size_bytes ? next : null};
    if(part) return {structuredContent:info,content:[],_meta:{[`split/${kind}Part`]:blob}};
    return {structuredContent:info,content:[{type:"text",text:JSON.stringify(info)},
      complete && kind==="audio" ? {type:"audio",mimeType:ref.mime_type,data:blob} :
      {type:"resource",annotations:{audience:["user"]},resource:{uri:metadata.url+(complete ? "" : `#bytes=${offset}-${next-1}`),
        mimeType:complete ? ref.mime_type : "application/octet-stream",blob}}]};
  } catch(e) {return failure(e);} // Never splice original bytes into an in-progress copy read.
}

const outputPageSchema=z.object({items:z.array(outputFileSchema.extend({index:z.number().int().nonnegative(),
  kind:z.enum(["image","audio","video","3d"])}).passthrough()),
  next_offset:z.number().int().nonnegative().nullable(),total:z.number().int().nonnegative()}).passthrough();
export async function deliveryOutputs(adapter:MediaDelivery,send:MediaSend,value:unknown,prepare:boolean,origin:string) {
  const page=outputPageSchema.parse(value);
  const items:Record<string,unknown>[]=page.items.map(file=>({...file,url:new URL(outputPath({filename:file.filename,subfolder:file.subfolder}),origin).href}));
  if(prepare) {
    for(let i=0;i<items.length;i++) {
      try {Object.assign(items[i],summary(await adapter.prepare(send,page.items[i])));}
      catch(e) {const p=problem(e);Object.assign(items[i],{delivery_status:"unavailable",delivery_reason:p.code,...(p.retry ? {retry_after_seconds:p.retry} : {})});}
    }
  } else {
    const sources:SourceMedia[]=[],positions:number[]=[];
    for(let i=0;i<items.length;i++) {
      try {sources.push(await adapter.inspect(send,page.items[i]));positions.push(i);}
      catch(e) {Object.assign(items[i],{delivery_status:"unavailable",delivery_reason:problem(e).code});}
    }
    try {
      const states=await adapter.lookup(sources);
      states.forEach((state,i)=>Object.assign(items[positions[i]],summary(state)));
    } catch(e) {for(const i of positions) Object.assign(items[i],{delivery_status:"unavailable",delivery_reason:problem(e).code});}
  }
  return {...page,items};
}
