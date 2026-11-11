import type {CallToolResult} from "@modelcontextprotocol/sdk/types.js";
import {z} from "zod";
import {readAtMost} from "./bounded-response.ts";
import {base64} from "./media.ts";

export const THREE_PART_BYTES = 512 * 1024;
export const THREE_INLINE_LIMIT = 128 * 1024 * 1024;
const PROBE_BYTES = 4096;
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
};
export const threeOutput = {
  filename:z.string(), subfolder:z.string(), format:z.enum(["glb","spz"]),
  mimeType:z.enum(["model/gltf-binary","application/octet-stream"]),
  size:z.number().int().positive(), url:z.url(), delivery:z.enum(["viewer","link","chunk"]),
  byte_offset:z.number().int().nonnegative().optional(),
  byte_length:z.number().int().positive().optional(),
  next_offset:z.number().int().nonnegative().nullable().optional(),
};
const schema = z.object(threePartInputs);
type Send = (path:string, init:RequestInit) => Promise<Response>;
const failure = (text:string):CallToolResult => ({isError:true, content:[{type:"text",text}]});

async function readRange(send:Send, path:string, offset:number, length:number):Promise<
  {bytes:Uint8Array; size:number} | {error:string}> {
  const response = await send(path, {method:"GET", headers:{Range:`bytes=${offset}-${offset + length - 1}`},
    signal:AbortSignal.timeout(50_000)});
  let error:string | undefined, size:number | undefined, expected:number | undefined;
  const contentLength = response.headers.get("Content-Length");
  const encoding = response.headers.get("Content-Encoding");
  if (response.status !== 200 && response.status !== 206) {
    error = response.status === 404 || response.status === 410 ?
      `Saved 3D file is missing or deleted (HTTP ${response.status}). No generation was submitted.` :
      response.status === 416 ? "Byte offset is outside the saved 3D file." :
      `Cannot read saved 3D file (HTTP ${response.status}).`;
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
  return {bytes, size:size ?? bytes.length};
}

export async function get3D(send:Send, origin:string, args:Record<string,unknown>, part = false):Promise<CallToolResult> {
  try {
    const parsed = schema.safeParse(part ? args : {...args, byte_offset:0});
    if (!parsed.success) return failure("Invalid 3D output filename, subfolder or byte_offset.");
    const {filename, subfolder = "", byte_offset:offset} = parsed.data;
    const format = /\.(glb|spz)$/i.exec(filename)?.[1].toLowerCase();
    if (!format) return failure("Supported 3D output formats: GLB and SPZ.");
    const path = "/view?" + new URLSearchParams({type:"output", filename, subfolder});
    const url = new URL(path, origin).href;
    const probe = await readRange(send, path, 0, PROBE_BYTES);
    if ("error" in probe) return failure(probe.error);
    const {bytes, size} = probe;
    if (format === "glb") {
      const header = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      if (bytes.length < 12 || header.getUint32(0, true) !== 0x46546c67 ||
          header.getUint32(4, true) !== 2 || header.getUint32(8, true) !== size)
        return failure("Saved GLB has an invalid magic, version or total-size header.");
    } else {
      // Official format: https://github.com/nianticlabs/spz/blob/main/src/cc/load-spz.cc
      // v1–3: gzip containing a 16-byte NGSP header; v4: plaintext 32-byte NGSP
      // header plus ZSTD streams. This checks headers, NOT the complete payload,
      // gzip trailer/CRC or ZSTD streams. Gzip magic alone is not an SPZ signature.
      const legacy = bytes[0] === 0x1f && bytes[1] === 0x8b;
      let headerBytes = bytes;
      if (legacy) {
        // Only the bounded compressed probe enters the decompressor. Stop at the
        // header; never inflate a whole file or feed a 512 KiB part into it.
        const reader = new Response(bytes).body!.pipeThrough(new DecompressionStream("gzip")).getReader();
        headerBytes = new Uint8Array(16);
        let used = 0;
        try {
          while (used < headerBytes.length) {
            const {done, value} = await reader.read();
            if (done) break;
            const prefix = value.subarray(0, headerBytes.length - used);
            headerBytes.set(prefix, used); used += prefix.length;
          }
          await reader.cancel();
        } finally { reader.releaseLock(); }
        if (used < 16) return failure("Saved SPZ has a truncated gzip/NGSP header.");
      }
      const header = new DataView(headerBytes.buffer, headerBytes.byteOffset, headerBytes.byteLength);
      if (headerBytes.length < (legacy ? 16 : 32) || header.getUint32(0, true) !== 0x5053474e)
        return failure("Saved SPZ does not contain a supported NGSP header.");
      const version = header.getUint32(4, true);
      if (legacy ? version < 1 || version > 3 : version !== 4)
        return failure("Unsupported SPZ header version (expected gzip v1–3 or plaintext v4).");
      if (!legacy) {
        const toc = header.getUint32(16, true);
        if (toc < 32 || toc + headerBytes[15] * 16 > size)
          return failure("Saved SPZ has an invalid or truncated table-of-contents header.");
      }
    }
    const metadata = {filename, subfolder, format, mimeType:format === "glb" ? "model/gltf-binary" : "application/octet-stream",
      size, url};
    if (!part) return {structuredContent:{...metadata, delivery:size <= THREE_INLINE_LIMIT ? "viewer" : "link"},
      content:[{type:"text",text:size <= THREE_INLINE_LIMIT ?
        filename + " — use the 3D viewer. Retrieval alone does not confirm the viewer is visible. " +
          "Open or save this file after website login: " + url :
        filename + " exceeds the 128 MiB viewer limit. Open or save it after website login: " + url}]};
    if (offset >= size) return failure("Byte offset is outside the saved 3D file.");
    const chunk = await readRange(send, path, offset, THREE_PART_BYTES);
    if ("error" in chunk) return failure(chunk.error);
    if (chunk.size !== size) return failure("Saved 3D file size changed during retrieval. Retry the read; no generation was submitted.");
    const next = offset + chunk.bytes.length;
    return {structuredContent:{...metadata, delivery:"chunk", byte_offset:offset,
      byte_length:chunk.bytes.length, next_offset:next < size ? next : null},
      content:[], _meta:{"split/3dPart":base64(chunk.bytes)}};
  } catch {
    return failure("Saved 3D delivery is unavailable or the response is invalid. Retry the read; no generation was submitted.");
  }
}
