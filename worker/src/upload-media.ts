import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { readAtMost } from "./bounded-response.ts";

const MAX = 256 * 1024 ** 2, PART = 8 * 1024 ** 2, CLI_PART = 1024 ** 2;
const file = z.object({download_url:z.string(), file_id:z.string().min(1).max(512),
  mime_type:z.string().optional(), file_name:z.string().optional()}).strict();
export const uploadInputs = {
  file: file.optional().describe("ChatGPT attachment; the client provides this object."),
  upload_id: z.string().regex(/^[0-9a-f]{64}$/).optional(),
  filename: z.string().min(1).max(220).optional(),
  offset: z.number().int().min(0).max(MAX).optional(),
  data: z.string().min(4).max(4*Math.ceil(CLI_PART/3)).optional().describe("CLI only: a base64 part of at most 1 MiB."),
  total_bytes: z.number().int().min(1).max(MAX).optional().describe("CLI only: complete the transfer at this exact size."),
};
const schema = z.object(uploadInputs).strict();
type Send = (path:string, init:RequestInit)=>Promise<Response>;
type Receipt = { upload_id:string; next_offset:number; complete:boolean; filename?:string };

async function update(send:Send, id:string, op:string, fields:Record<string,string>, body?:Uint8Array):Promise<Receipt> {
  const query = new URLSearchParams({id,op,...fields});
  const res = await send("/_remote_mcp/media?"+query, {method:"POST", headers:{
    "X-Split-Remote-Tool":"1","Content-Type":"application/octet-stream"}, body, signal:AbortSignal.timeout(45_000)});
  const bytes = await readAtMost(res, 4096);
  if (!res.ok) throw new Error(res.status >= 500 ? "Split transfer unavailable; retry with the same input." :
    "Upload rejected ("+res.status+"): "+new TextDecoder().decode(bytes ?? new Uint8Array()).slice(0,220));
  return JSON.parse(new TextDecoder().decode(bytes!));
}

function attachmentUrl(value:string, blobHosts = "") {
  const u = new URL(value);
  // Azure is multi-tenant: only operator-verified accounts with a signed download URL.
  const azure = /^[a-z0-9]{3,24}\.blob\.core\.windows\.net$/.test(u.hostname)
    && blobHosts.split(",").some(h=>h.trim().toLowerCase()===u.hostname) && !!u.searchParams.get("sig");
  if (u.protocol !== "https:" || u.port || u.username || u.password || (!u.hostname.endsWith(".oaiusercontent.com") && !azure))
    throw new Error("Unapproved attachment origin");
  return u;
}

async function attachment(send:Send, item:z.infer<typeof file>, download:typeof fetch, blobHosts?:string) {
  const url = attachmentUrl(item.download_url, blobHosts);
  const hash = await crypto.subtle.digest("SHA-256",new TextEncoder().encode("chatgpt-file:"+item.file_id));
  const id = Array.from(new Uint8Array(hash),v=>v.toString(16).padStart(2,"0")).join("");
  const initial = await update(send,id,"status",{name:item.file_name || "attachment"});
  if (initial.complete) return initial;
  const start = initial.next_offset;
  // Never forward caller or origin credentials. Never follow a redirect with a signed URL.
  const res = await download(url, {headers:start ? {Range:"bytes="+start+"-"} : {},
    redirect:"manual",signal:AbortSignal.timeout(90_000)});
  if (res.status===416 && start>0 && res.headers.get("Content-Range")==="bytes */"+start)
    return update(send,id,"finish",{total:String(start)});
  const range = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(res.headers.get("Content-Range") || "");
  const partial = res.status === 206;
  let offset = partial ? start : 0;
  const sizeHeader = res.headers.get("Content-Length");
  const expected = partial && range ? Number(range[3]) : sizeHeader === null ? null : Number(sizeHeader);
  if ((res.status !== 200 && !partial) || !res.body || (partial && (!range || Number(range[1])!==start ||
      Number(range[2])+1!==expected)) || (expected!==null && (!Number.isSafeInteger(expected) || expected<=0 || expected>MAX))) {
    await res.body?.cancel();
    throw new Error("Attachment unavailable, expired, oversized, or invalid range");
  }
  const reader=res.body.getReader(), buf=new Uint8Array(PART);
  let used=0;
  try {
    for (;;) {
      const {done,value}=await reader.read();
      if (done) break;
      if (offset+used+value.length>MAX) throw new Error("Attachment exceeds 256 MiB");
      for (let i=0;i<value.length;) {
        const n=Math.min(PART-used,value.length-i);
        buf.set(value.subarray(i,i+n),used); i+=n; used+=n;
        if (used===PART) {
          await update(send,id,"part",{offset:String(offset)},buf);
          offset+=used; used=0;
        }
      }
    }
    if (used) { await update(send,id,"part",{offset:String(offset)},buf.subarray(0,used)); offset+=used; }
    if (!offset || (expected!==null && offset!==expected)) throw new Error("Incomplete attachment; retry the same file");
    return await update(send,id,"finish",{total:String(offset)});
  } finally { await reader.cancel(); reader.releaseLock(); }
}

export async function uploadMedia(send:Send, raw:unknown, download=fetch, blobHosts?:string):Promise<CallToolResult> {
  try {
    const a=schema.parse(raw);
    let receipt:Receipt;
    if (a.file) {
      if (Object.keys(a).length!==1) throw new Error("Pass only the ChatGPT attachment");
      receipt=await attachment(send,a.file,download,blobHosts);
    } else {
      if (!a.upload_id) throw new Error("Missing upload_id");
      if (a.data!==undefined && a.offset!==undefined && a.total_bytes===undefined && !a.filename) {
        const str=atob(a.data);
        if (str.length>CLI_PART || !str.length || btoa(str)!==a.data) throw new Error("Invalid base64 part");
        receipt=await update(send,a.upload_id,"part",{offset:String(a.offset)},Uint8Array.from(str,c=>c.charCodeAt(0)));
      } else if (Object.keys(a).length===2 && a.filename) {
        receipt=await update(send,a.upload_id,"status",{name:a.filename});
      } else if (Object.keys(a).length===2 && a.total_bytes) {
        receipt=await update(send,a.upload_id,"finish",{total:String(a.total_bytes)});
      } else throw new Error("Use filename to start/resume, offset and data for a part, or total_bytes to finish");
    }
    return {content:[{type:"text",text:JSON.stringify(receipt)}]};
  } catch (error) {
    // Fetch exceptions can include the signed URL; surface only our own sanitized messages.
    const safe=error instanceof Error && /^(?:Unapproved |Upload rejected|Split transfer|Attachment |Incomplete attachment|Missing upload_id|Invalid base64|Use filename|Pass only)/.test(error.message);
    return {isError:true,content:[{type:"text",text:safe?(error as Error).message:"Upload failed; retry the same file or transfer ID."}]};
  }
}
