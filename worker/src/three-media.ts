import type {CallToolResult} from "@modelcontextprotocol/sdk/types.js";
import {z} from "zod";
import {readAtMost} from "./bounded-response.ts";
import {base64} from "./media.ts";

export const THREE_PART_BYTES = 512 * 1024;
export const THREE_INLINE_LIMIT = 128 * 1024 * 1024;
const strongETag = z.string().max(512).regex(/^"[\x21\x23-\x7e]*"$/);
const safeSegment = (value:string) => value.length > 0 && value.length <= 255 &&
  !value.startsWith(".") && !/[/\\:\x00-\x1f\x7f]/.test(value);
export const threeInputs = {
  filename:z.string().refine(safeSegment, "Expected a safe output basename without a dot prefix")
    .describe("Saved GLB or SPZ output filename"),
  subfolder:z.string().max(1024).refine(value => !value || value.split("/").every(safeSegment),
    "Expected slash-separated safe output folders").optional(),
};
export const threePartInputs = {...threeInputs,
  byte_offset:z.number().int().min(0).max(Number.MAX_SAFE_INTEGER - THREE_PART_BYTES)
    .describe("Byte offset of the next private 512 KiB part"),
  etag:strongETag.optional().describe("File version from get_3d or the first part; required after byte offset 0"),
};
export const threeOutput = {
  filename:z.string(), subfolder:z.string(), format:z.enum(["glb","spz"]),
  mimeType:z.enum(["model/gltf-binary","application/octet-stream"]),
  size:z.number().int().positive(), url:z.url(), delivery:z.enum(["viewer","link","chunk"]),
  byte_offset:z.number().int().nonnegative().optional(),
  byte_length:z.number().int().positive().optional(),
  next_offset:z.number().int().nonnegative().nullable().optional(),
  etag:strongETag.optional(),
};
const schema = z.object(threePartInputs);
type Send = (path:string, init:RequestInit) => Promise<Response>;
const failure = (text:string):CallToolResult => ({isError:true, content:[{type:"text",text}]});

async function readRange(send:Send, path:string, offset:number, length:number, ifRange?:string):Promise<
  {bytes:Uint8Array; size:number; etag?:string} | {error:string}> {
  const headers:Record<string,string> = {Range:`bytes=${offset}-${offset + length - 1}`};
  if (ifRange) headers["If-Range"] = ifRange;
  const response = await send(path, {method:"GET", headers,
    signal:AbortSignal.timeout(50_000)});
  let error:string | undefined, size:number | undefined, expected:number | undefined;
  const contentLength = response.headers.get("Content-Length");
  const encoding = response.headers.get("Content-Encoding");
  const tag = strongETag.safeParse(response.headers.get("ETag"));
  const etag = tag.success ? tag.data : undefined;
  if (response.status !== 200 && response.status !== 206) {
    error = response.status === 404 || response.status === 410 ?
      `Saved 3D file is missing or deleted (HTTP ${response.status}). No generation was submitted.` :
      response.status === 416 ? "Byte offset is outside the saved 3D file." :
      response.status === 503 ? "Saved 3D delivery is temporarily unavailable (HTTP 503). Retry the read shortly; no generation was submitted." :
      `Cannot read saved 3D file (HTTP ${response.status}).`;
  } else if (ifRange && (response.status !== 206 || etag !== ifRange)) {
    error = "Saved 3D file version changed or its range could not be verified. Retry get_3d; no generation was submitted.";
  } else if (encoding && encoding.toLowerCase() !== "identity") {
    error = "Unexpected content encoding in the 3D byte range.";
  } else if (response.status === 206) {
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get("Content-Range") || "");
    if (!match || !Number.isSafeInteger(size = Number(match[3])) || size <= offset ||
        Number(match[1]) !== offset || Number(match[2]) !== Math.min(offset + length, size) - 1) {
      error = "Invalid upstream 3D byte range.";
    } else expected = Math.min(length, size - offset);
  } else if (offset !== 0 || response.headers.has("Content-Range")) {
    error = "Upstream did not honor the 3D byte range.";
  }
  if (!error && contentLength !== null) {
    const count = Number(contentLength);
    if (!/^\d+$/.test(contentLength) || !Number.isSafeInteger(count) || count <= 0 ||
        count > length || (expected !== undefined && count !== expected)) {
      error = "Invalid 3D response length or upstream ignored the byte range.";
    } else expected = count;
  }
  if (error) { await response.body?.cancel(); return {error}; }
  const bytes = await readAtMost(response, length);
  if (!bytes) return {error:"3D response exceeds the requested byte range."};
  if (!bytes.length || (expected !== undefined && bytes.length !== expected))
    return {error:"Incomplete 3D byte range response."};
  return {bytes, size:size ?? bytes.length, etag};
}

export async function get3D(send:Send, origin:string, args:Record<string,unknown>, part = false):Promise<CallToolResult> {
  const parsed = schema.safeParse(part ? args : {...args, byte_offset:0});
  if (!parsed.success) return failure("Invalid 3D output filename, subfolder, byte_offset or etag.");
  const {filename, subfolder = "", byte_offset:offset, etag:expectedETag} = parsed.data;
  if (part && offset > 0 && !expectedETag)
    return failure("Pass the file etag from get_3d or the first part when continuing a 3D read.");
  const format = /\.(glb|spz)$/i.exec(filename)?.[1].toLowerCase();
  if (!format) return failure("Supported 3D output formats: GLB and SPZ.");
  const path = "/view?" + new URLSearchParams({type:"output", filename, subfolder});
  const url = new URL(path, origin).href;
  // Metadata needs one byte for Content-Range. Each private read fetches only
  // its requested part. GLB/SPZ contents are parsed at the viewer boundary.
  const chunk = await readRange(send, path, offset, part ? THREE_PART_BYTES : 1, part ? expectedETag : undefined);
  if ("error" in chunk) return failure(chunk.error);
  const {bytes, size, etag} = chunk;
  const metadata = {filename, subfolder, format, mimeType:format === "glb" ? "model/gltf-binary" : "application/octet-stream",
    size, url, ...(etag ? {etag} : {})};
  if (!part) return {structuredContent:{...metadata, delivery:etag && size <= THREE_INLINE_LIMIT ? "viewer" : "link"},
    content:[{type:"text",text:size > THREE_INLINE_LIMIT ?
      filename + " exceeds the 128 MiB preview limit in both chat and browser. Sign in and save the original file from this page: " + url : !etag ?
      filename + " has no verifiable file version. Open or save it after website login: " + url :
      filename + " — use the 3D viewer. Retrieval alone does not confirm the viewer is visible. " +
        "Open or save this file after website login: " + url}]};
  if (!etag) return failure("Saved 3D file has no verifiable ETag. Use the signed-in browser link.");
  const next = offset + bytes.length;
  return {structuredContent:{...metadata, delivery:"chunk", byte_offset:offset,
    byte_length:bytes.length, next_offset:next < size ? next : null},
    content:[], _meta:{"split/3dPart":base64(bytes)}};
}
