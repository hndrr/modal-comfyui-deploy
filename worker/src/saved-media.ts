import type {CallToolResult} from "@modelcontextprotocol/sdk/types.js";
import {z} from "zod";
import {readAtMost} from "./bounded-response.ts";
import {AUDIO_PART_BYTES, audioPartInputs, audioSchema, base64, getMedia, videoSchema} from "./media.ts";

type Send = (path:string, init:RequestInit) => Promise<Response>;
const failure=(text:string):CallToolResult=>({isError:true,content:[{type:"text",text}]});
export const savedMediaTools=new Set(["get_image","get_audio","get_video","read_audio_part","list_outputs"]);

// Credentials and the fixed origin are deployment configuration, never tool arguments.
export function mediaSender(raw:string, hostname:string, credentials:Headers):Send {
  const cfg=JSON.parse(raw), url=new URL(cfg.origin);
  const key=credentials.get("Modal-Key"),secret=credentials.get("Modal-Secret");
  if (cfg.hostname!==hostname || url.protocol!=="https:" || !url.hostname.endsWith(".modal.run") ||
      url.port || url.username || url.password || url.pathname!=="/" || url.search || url.hash ||
      !key || !secret)
    throw new Error("Invalid saved-media binding");
  return (path,init)=>{
    const target=new URL(path,url.origin);
    if (target.origin!==url.origin || !/^\/(?:view|api\/view|history\/[0-9a-f-]+|outputs\/[0-9a-f-]+|catalog\/(?:models|workflows|history|job-workflow))$/.test(target.pathname) ||
        !["GET","HEAD"].includes(init.method || "GET")) throw new Error("Invalid media operation");
    const headers=new Headers({"Modal-Key":key,"Modal-Secret":secret,"X-Modal-Execution-Mode":"split"});
    const supplied=new Headers(init.headers);
    for (const key of ["Range","If-Range","If-None-Match"])
      if (supplied.has(key)) headers.set(key,supplied.get(key)!);
    return fetch(new Request(target,{method:init.method || "GET",headers,signal:init.signal,redirect:"manual"}));
  };
}

// Keep original browser requests outside these formats on the ComfyUI route.
const savedExtensions=new Set(["png","jpg","jpeg","webp","gif","mp3","wav","flac","ogg","opus","m4a","mp4","mov","webm","mkv"]);
export function savedOutputRequest(request:Request):boolean {
  const url=new URL(request.url),q=url.searchParams;
  return ["GET","HEAD"].includes(request.method) && ["/view","/api/view"].includes(url.pathname) &&
    (q.get("type") || "output")==="output" && !q.has("preview") && !q.has("channel") &&
    savedExtensions.has((q.get("filename") || "").split(".").at(-1)!.toLowerCase());
}

async function image(send:Send,origin:string,args:Record<string,unknown>):Promise<CallToolResult> {
  const segment=(v:unknown):v is string=>typeof v==="string" && v.length>0 && v.length<=255 &&
    !v.startsWith(".") && !/[/\\:\x00-\x1f\x7f]/.test(v);
  const {filename,subfolder=""}=args;
  if (!segment(filename) || typeof subfolder!=="string" || subfolder.length>1024 ||
      (subfolder && !subfolder.split("/").every(segment))) return failure("Invalid output image name.");
  const ext=filename.split(".").at(-1)!.toLowerCase();
  const mime=({png:"image/png",jpg:"image/jpeg",jpeg:"image/jpeg",webp:"image/webp"} as Record<string,string>)[ext];
  if (!mime) return failure("Supported output images: PNG, JPEG and WebP.");
  const path="/view?"+new URLSearchParams({type:"output",filename,subfolder});
  const response=await send(path,{method:"GET",signal:AbortSignal.timeout(50_000)});
  if (response.status!==200) {await response.body?.cancel();return failure("Cannot read saved image (HTTP "+response.status+").");}
  const bytes=await readAtMost(response,8*1024*1024);
  const url=new URL(path,origin).href;
  if (!bytes) return {content:[{type:"text",text:"Open the saved image after signing in: "+url},
    {type:"resource_link",uri:url,name:filename,mimeType:mime}]};
  const tag=(at:number,s:string)=>s.split("").every((c,i)=>bytes[at+i]===c.charCodeAt(0));
  if (!(ext==="png" ? bytes[0]===137 && tag(1,"PNG\r\n\x1a\n") :
        ext==="webp" ? tag(0,"RIFF") && tag(8,"WEBP") : bytes[0]===255 && bytes[1]===216 && bytes[2]===255))
    return failure("Saved image content does not match its format.");
  return {content:[{type:"image",mimeType:mime,data:base64(bytes)}]};
}

export async function callSavedMedia(send:Send,name:string,args:Record<string,unknown>,origin:string):Promise<CallToolResult> {
  try {
    if(typeof args.prompt_id==="string")args={...args,prompt_id:args.prompt_id.toLowerCase()};
    if (name==="get_image") return await image(send,origin,args);
    if (name==="list_outputs") {
      const values=z.object({prompt_id:z.uuid(),offset:z.number().int().min(0).default(0),
        limit:z.number().int().min(1).max(100).default(50)}).parse(args);
      const r=await send("/outputs/"+values.prompt_id+"?"+new URLSearchParams({offset:String(values.offset),limit:String(values.limit)}),
        {method:"GET",signal:AbortSignal.timeout(50_000)});
      const bytes=await readAtMost(r,1024*1024);
      if (!bytes) return failure("Output metadata exceeds the read limit.");
      return {isError:!r.ok,content:[{type:"text",text:JSON.stringify(JSON.parse(new TextDecoder().decode(bytes)))}]};
    }
    if (name==="read_audio_part") return await getMedia("audio",send,origin,
      {...z.object(audioPartInputs).parse(args),chunk_bytes:AUDIO_PART_BYTES},true);
    return name==="get_audio" ? await getMedia("audio",send,origin,audioSchema.parse(args)) :
      await getMedia("video",send,origin,videoSchema.parse(args));
  } catch {
    return failure("Saved-media delivery is unavailable. Retry the read; no generation was submitted.");
  }
}
