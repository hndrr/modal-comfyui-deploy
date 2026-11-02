// A split gateway extension. No dependency on the upstream stdio server.
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

const MAX_PART = 4 * 1024 * 1024;
export const videoInputs = {
  prompt_id: z.uuid().describe("Completed split job ID"),
  index: z.number().int().nonnegative().optional().describe("Zero-based MP4 index (default 0)"),
  byte_offset: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER - MAX_PART).optional()
    .describe("Return a binary chunk starting at this byte; follow next_offset until null"),
  chunk_bytes: z.number().int().min(1).max(MAX_PART).optional()
    .describe("Chunk size in bytes, up to 4 MiB; implies chunk mode"),
};
export const videoSchema = z.object(videoInputs);
type Send = (path: string, init: RequestInit) => Promise<Response>;
type Output = { filename: string; subfolder: string };
const fail = (text: string): CallToolResult => ({isError:true, content:[{type:"text",text}]});

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function safeSegment(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 255 &&
    value !== "." && value !== ".." && !/[/\\:\x00-\x1f\x7f]/.test(value);
}
function videos(outputs: unknown): Output[] {
  const result: Output[] = [];
  if (!record(outputs)) return result;
  for (const node of Object.values(outputs)) {
    if (!record(node)) continue;
    for (const list of Object.values(node)) {
      if (!Array.isArray(list)) continue;
      for (const item of list) {
        if (!record(item) || item.type !== "output" || !safeSegment(item.filename) ||
            !/\.mp4$/i.test(item.filename)) continue;
        const subfolder = item.subfolder ?? "";
        if (typeof subfolder !== "string" || subfolder.length > 1024 ||
            (subfolder && !subfolder.split("/").every(safeSegment))) continue;
        result.push({filename:item.filename, subfolder});
      }
    }
  }
  return result;
}

// Neither Content-Length nor upstream range handling is assumed. Stop reading
// as soon as a bounded result cannot fit; never buffer a full large video.
async function readAtMost(response: Response, limit: number): Promise<Uint8Array | null> {
  if (Number(response.headers.get("Content-Length")) > limit) {
    await response.body?.cancel();
    return null;
  }
  const reader = response.body?.getReader();
  if (!reader) return new Uint8Array();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) { await reader.cancel(); return null; }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

function base64(bytes: Uint8Array): string {
  const parts = [];
  // A multiple of three keeps padding confined to the very last block.
  for (let i=0; i<bytes.length; i+=24576)
    parts.push(btoa(String.fromCharCode(...bytes.subarray(i,i+24576))));
  return parts.join("");
}

export async function getVideo(send: Send, publicOrigin: string,
  {prompt_id, index = 0, byte_offset, chunk_bytes}: z.infer<typeof videoSchema>): Promise<CallToolResult> {
  const get = (path: string, headers?: HeadersInit) =>
    send(path, {method:"GET", headers, signal:AbortSignal.timeout(50_000)});
  const history = await get("/history/" + prompt_id);
  if (history.status !== 200) return fail("Cannot read this job's history (HTTP " + history.status + ").");
  const bytes = await readAtMost(history, 8 * 1024 * 1024);
  if (!bytes) return fail("Job history exceeds the read limit.");
  const jobs = JSON.parse(new TextDecoder().decode(bytes));
  const job = jobs[prompt_id];
  if (!job) return fail("No finished history for this prompt_id. Check queue status; do not resubmit.");
  if (job.status?.status_str !== "success" || job.status.completed !== true)
    return fail("This job did not finish successfully.");
  const files = videos(job.outputs);
  if (!files[index]) return fail("This job has " + files.length + " MP4 output(s); no video at index " + index + ".");
  const {filename, subfolder} = files[index];
  const path = "/view?" + new URLSearchParams({type:"output", filename, subfolder});
  const url = new URL(path, publicOrigin).href;
  const offset = byte_offset ?? 0, length = chunk_bytes ?? MAX_PART;
  const chunks = byte_offset !== undefined || chunk_bytes !== undefined;
  const file = await get(path, {Range:`bytes=${offset}-${offset + length - 1}`});
  const metadata = {prompt_id, filename, index, video_count:files.length, url};
  const link = (size?: number): CallToolResult => ({
    structuredContent:{...metadata, delivery:"link", size},
    content:[{type:"text",text:"Open the MP4 after signing in: " + url +
      "\nFor MCP-only download, call get_video with byte_offset: 0 and follow next_offset."},
      {type:"resource_link", uri:url, name:filename, mimeType:"video/mp4", size}],
  });
  if (file.status !== 200 && file.status !== 206) {
    await file.body?.cancel();
    return fail(file.status === 416 ? "Byte offset is outside this video." :
      "Cannot retrieve the saved video (HTTP " + file.status + ").");
  }
  let total: number | undefined, expected: number | undefined;
  if (file.status === 206) {
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(file.headers.get("Content-Range") || "");
    if (!match || !Number.isSafeInteger(total = Number(match[3])) || Number(match[1]) !== offset ||
        total <= offset || Number(match[2]) !== Math.min(offset + length, total) - 1) {
      await file.body?.cancel();
      return fail("Invalid upstream video range.");
    }
    expected = Math.min(length, total - offset);
    if (!chunks && total > length) { await file.body?.cancel(); return link(total); }
  } else if (offset > 0) {
    await file.body?.cancel();
    return fail("Upstream ignored the byte offset. Open the authenticated video URL: " + url);
  }
  const data = await readAtMost(file, length);
  if (!data) return chunks ? fail("Upstream did not honor the byte range. Open the authenticated video URL: " + url) : link();
  if (!data.length || (expected !== undefined && expected !== data.length))
    return fail("Incomplete MP4 response.");
  total ??= data.length;
  if (total < 8) return fail("The output is not an MP4 file.");
  if (offset === 0 && data.length >= 8 && String.fromCharCode(...data.subarray(4,8)) !== "ftyp")
    return fail("The output is not an MP4 file.");
  const complete = offset === 0 && data.length === total;
  const next = offset + data.length < total ? offset + data.length : null;
  const info = {...metadata, delivery:complete ? "inline" : "chunk", size:total,
    byte_offset:offset, byte_length:data.length, next_offset:next};
  return {structuredContent:info, content:[
    {type:"text", text:JSON.stringify(info)},
    {type:"resource", annotations:{audience:["user"]}, resource:{
      uri:complete ? url : url + `#bytes=${offset}-${offset + data.length - 1}`,
      mimeType:complete ? "video/mp4" : "application/octet-stream", blob:base64(data),
    }},
  ]};
}
