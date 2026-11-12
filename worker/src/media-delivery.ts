import {z} from "zod";
import {readAtMost} from "./bounded-response.ts";
import {mediaHeaderValid} from "./media.ts";

export type MediaSend = (path:string, init:RequestInit) => Promise<Response>;
type Fetch = (request:Request) => Promise<Response>;
const MAX_BYTES = 25 * 1024 * 1024;
const mimeKinds = {
  "image/png":"image", "image/jpeg":"image", "image/webp":"image", "image/gif":"image",
  "audio/wav":"audio", "audio/mpeg":"audio", "audio/mp4":"audio", "audio/ogg":"audio",
  "audio/flac":"audio", "audio/webm":"audio", "video/mp4":"video", "video/webm":"video",
  "model/gltf-binary":"3d", "application/octet-stream":"3d",
} as const;
export type MediaKind = typeof mimeKinds[keyof typeof mimeKinds];
const mimeSchema = z.enum(Object.keys(mimeKinds) as [keyof typeof mimeKinds, ...Array<keyof typeof mimeKinds>]);
const extensions:Record<string,keyof typeof mimeKinds> = {
  png:"image/png",jpg:"image/jpeg",jpeg:"image/jpeg",webp:"image/webp",gif:"image/gif",
  wav:"audio/wav",mp3:"audio/mpeg",m4a:"audio/mp4",ogg:"audio/ogg",opus:"audio/ogg",flac:"audio/flac",
  mp4:"video/mp4",webm:"video/webm",glb:"model/gltf-binary",spz:"application/octet-stream",
};
const safeSegment = (s:string) => s.length > 0 && s.length <= 255 && !s.startsWith(".") && !/[/\\:\x00-\x1f\x7f]/.test(s);
export const outputFileSchema = z.object({
  filename:z.string().refine(safeSegment),
  subfolder:z.string().default("").refine(s => s.length <= 768 && (!s || s.split("/").every(safeSegment))),
});
export type OutputFile = z.infer<typeof outputFileSchema>;
const etagSchema = z.string().max(256).regex(/^"[\x21\x23-\x7e]*"$/);
export const deliveryRefSchema = outputFileSchema.extend({
  media_id:z.uuid(), etag:etagSchema, mime_type:mimeSchema,
  size_bytes:z.number().int().positive().max(MAX_BYTES),
  prompt_id:z.uuid().optional(), index:z.number().int().nonnegative().optional(),
});
export type DeliveryRef = z.infer<typeof deliveryRefSchema>;
export const deliveryOutput = {delivery_ref:deliveryRefSchema.optional(),
  delivery_route:z.literal("original").optional(),
  delivery_status:z.enum(["ready","pending","unavailable"]).optional(),
  delivery_reason:z.string().optional(), retry_after_seconds:z.number().int().positive().optional()};
const readySchema = z.object({status:z.literal("ready"),media_id:z.uuid(),url:z.url(),
  etag:etagSchema,mime_type:mimeSchema,size_bytes:z.number().int().positive().max(MAX_BYTES),
  expires_at:z.iso.datetime().nullable(),cleansed:z.boolean()});
const stateSchema = z.discriminatedUnion("status",[
  readySchema,
  z.object({status:z.literal("missing"),reason:z.enum(["not_found","expired","upload_failed"]),media_id:z.uuid().optional()}),
  z.object({status:z.literal("pending"),media_id:z.uuid(),retry_after_seconds:z.number().int().positive().max(86400)}),
  z.object({status:z.literal("upload_required"),media_id:z.uuid(),upload_id:z.uuid()}),
]);
export type DeliveryState = z.infer<typeof stateSchema>;
export type ReadyMedia = z.infer<typeof readySchema>;
export type SourceMedia = OutputFile & {output_path:string; source_etag:string; mime_type:keyof typeof mimeKinds; size_bytes:number; kind:MediaKind; path:string};
export class DeliveryError extends Error {
  code:string;
  retry:number;
  constructor(code:string, retry = 0) {super(code);this.code=code;this.retry=retry;}
}
export const outputPath = ({filename,subfolder}:OutputFile) => "/view?" + new URLSearchParams({type:"output",filename,subfolder});
export const mediaKind = (mime:keyof typeof mimeKinds) => mimeKinds[mime];
const contentType = (response:Response) => response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase();
// ComfyUI's Python MIME table uses these aliases; the service contract is canonical.
const sourceType = (response:Response) => {
  const mime=contentType(response);
  return ({"audio/x-wav":"audio/wav","audio/vnd.wave":"audio/wav","audio/x-flac":"audio/flac",
    "audio/opus":"audio/ogg","application/ogg":"audio/ogg"} as Record<string,string>)[mime ?? ""] ?? mime;
};

// Only this boundary knows service credentials. No bindings to the service's storage.
export class MediaDelivery {
  readonly origin:string;
  private source:string;
  private token:string;
  private fetcher:Fetch;
  private deadline=Date.now()+50_000;
  constructor(raw:string, fetcher:Fetch = request => fetch(request)) {
    const cfg=z.object({origin:z.url(),source_id:z.string().regex(/^[a-zA-Z0-9_-]{1,64}$/).refine(s=>s!=="local"),token:z.string().regex(/^[A-Za-z0-9._~-]{32,256}$/)}).strict().parse(JSON.parse(raw));
    const u=new URL(cfg.origin);
    if(u.protocol!=="https:" || u.username || u.password || u.port || u.pathname!=="/" || u.search || u.hash || /[\x00-\x20\x7f]/.test(cfg.token))
      throw new Error("Invalid media delivery binding");
    this.origin=u.origin;this.source=cfg.source_id;this.token=cfg.token;this.fetcher=fetcher;
  }
  url(id:string) {return `${this.origin}/media/${id}`;}
  private signal() {return AbortSignal.timeout(Math.max(0,this.deadline-Date.now()));}
  private async request(path:string, init:RequestInit = {}) {
    const headers=new Headers(init.headers);
    headers.set("Authorization","Bearer "+this.token);
    try {
      return await this.fetcher(new Request(this.origin+path,{...init,headers,redirect:"manual",
        signal:init.signal ?? this.signal(),...(init.body ? {duplex:"half"} : {})}));
    } catch {throw new DeliveryError("service_unavailable",60);}
  }
  private async json(path:string, init:RequestInit):Promise<unknown> {
    const response=await this.request(path,init);
    const bytes=await readAtMost(response,64*1024);
    if(!bytes) throw new DeliveryError("invalid_service_response");
    let data;
    try {data=JSON.parse(new TextDecoder().decode(bytes));}
    catch {throw new DeliveryError("invalid_service_response");}
    if(!response.ok) {
      const code=z.string().max(64).regex(/^[a-z_]+$/).safeParse(data?.error?.code);
      const retry=z.number().int().positive().max(86400).safeParse(data?.error?.retry_after_seconds);
      throw new DeliveryError(code.success ? code.data : "service_rejected",retry.success ? retry.data : 0);
    }
    return data;
  }
  private state(value:unknown):DeliveryState {
    const parsed=stateSchema.safeParse(value);
    if(!parsed.success) throw new DeliveryError("invalid_service_response");
    if(parsed.data.status==="ready" && parsed.data.url!==this.url(parsed.data.media_id))
      throw new DeliveryError("invalid_service_url");
    return parsed.data;
  }
  async inspect(send:MediaSend, file:OutputFile):Promise<SourceMedia> {
    const path=outputPath(file);
    const mime=extensions[file.filename.split(".").at(-1)!.toLowerCase()];
    if(!mime) throw new DeliveryError("unsupported_media");
    const response=await send(path,{method:"HEAD",signal:this.signal()});
    await response.body?.cancel();
    if(response.status!==200) throw new DeliveryError("source_unavailable");
    const size=Number(response.headers.get("Content-Length"));
    if(!Number.isSafeInteger(size) || size<=0) throw new DeliveryError("invalid_source_size");
    if(size>MAX_BYTES) throw new DeliveryError("file_too_large");
    const tag=etagSchema.safeParse(response.headers.get("ETag"));
    if(!tag.success) throw new DeliveryError("source_version_unavailable");
    const actual=sourceType(response);
    if(actual!==mime) throw new DeliveryError("unsupported_media");
    return {...file,path,output_path:(file.subfolder ? file.subfolder+"/" : "")+file.filename,
      source_etag:tag.data,mime_type:mime,size_bytes:size,kind:mediaKind(mime)};
  }
  async lookup(sources:SourceMedia[]):Promise<DeliveryState[]> {
    const results:DeliveryState[]=[];
    for(let offset=0;offset<sources.length;offset+=24) {
      const page=sources.slice(offset,offset+24);
      const body={source_id:this.source,references:page.map(({output_path,source_etag})=>({output_path,source_etag}))};
      const data=await this.json("/v1/media/lookup",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const parsed=z.object({results:z.array(z.unknown()).length(page.length)}).safeParse(data);
      if(!parsed.success) throw new DeliveryError("invalid_service_response");
      for(const [i,item] of parsed.data.results.entries()) {
        const state=this.state(item);
        if(state.status==="ready" && mediaKind(state.mime_type)!==page[i].kind) throw new DeliveryError("invalid_service_media");
        results.push(state);
      }
    }
    return results;
  }
  async prepare(send:MediaSend,file:OutputFile):Promise<DeliveryState> {
    const source=await this.inspect(send,file);
    return this.prepareSource(send,source,(await this.lookup([source]))[0]);
  }
  async prepareSource(send:MediaSend,source:SourceMedia,state:DeliveryState):Promise<DeliveryState> {
    if(state.status!=="ready" && state.status!=="pending") {
      const {output_path,source_etag,filename,mime_type,size_bytes,kind}=source;
      state=this.state(await this.json("/v1/media",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({source_id:this.source,output_path,source_etag,filename,mime_type,size_bytes,kind})}));
      if(state.status==="upload_required") {
        const response=await send(source.path,{method:"GET",headers:{Range:`bytes=0-${size_bytes-1}`,"If-Range":source_etag},signal:this.signal()});
        if(![200,206].includes(response.status) || response.headers.get("ETag")!==source_etag ||
          Number(response.headers.get("Content-Length"))!==size_bytes ||
          (response.status===206 && response.headers.get("Content-Range")!==`bytes 0-${size_bytes-1}/${size_bytes}`) ||
          sourceType(response)!==mime_type || !response.body) {
          await response.body?.cancel();throw new DeliveryError("source_changed");
        }
        // The receiver validates exact length while streaming; never hold a full file here.
        state=this.state(await this.json(`/v1/media/${state.media_id}/content`,{method:"PUT",
          headers:{"Content-Type":mime_type,"X-Upload-Id":state.upload_id,"Content-Length":String(size_bytes)},body:response.body}));
        const after=await this.inspect(send,source);
        if(after.source_etag!==source_etag || after.size_bytes!==size_bytes) throw new DeliveryError("source_changed");
      }
      if(state.status==="ready" && mediaKind(state.mime_type)!==source.kind) throw new DeliveryError("invalid_service_media");
    }
    return state;
  }
  async read(ref:DeliveryRef,offset:number,length:number):Promise<Uint8Array> {
    const response=await this.request(`/v1/media/${ref.media_id}/content`,{headers:{Range:`bytes=${offset}-${offset+length-1}`,"If-Range":ref.etag}});
    if(response.status!==206 || response.headers.get("ETag")!==ref.etag ||
      response.headers.get("Content-Range")!==`bytes ${offset}-${Math.min(offset+length,ref.size_bytes)-1}/${ref.size_bytes}` ||
      contentType(response)!==ref.mime_type ||
      (response.headers.has("Content-Encoding") && response.headers.get("Content-Encoding")!=="identity")) {
      await response.body?.cancel();
      throw new DeliveryError(response.status===503 ? "delivery_pending" : "copy_changed_or_unavailable",response.status===503 ? 60 : 0);
    }
    const bytes=await readAtMost(response,length);
    if(!bytes || bytes.length!==Math.min(length,ref.size_bytes-offset)) throw new DeliveryError("incomplete_delivery");
    if(offset===0 && !mediaHeaderValid(ref.mime_type,bytes,ref.size_bytes)) throw new DeliveryError("invalid_media_content");
    return bytes;
  }
}
