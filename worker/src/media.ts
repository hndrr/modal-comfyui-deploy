// A split gateway extension. No dependency on the upstream stdio server.
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { readAtMost } from "./bounded-response.ts";

const MAX_PART = 4 * 1024 * 1024;
const commonInputs = {
  prompt_id: z.uuid().describe("Completed split job ID"),
  byte_offset: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER - MAX_PART).optional()
    .describe("Return a binary chunk starting at this byte; follow next_offset until null"),
  chunk_bytes: z.number().int().min(1).max(MAX_PART).optional()
    .describe("Chunk size in bytes, up to 4 MiB; implies chunk mode"),
};
const outputIndex = z.number().int().nonnegative().optional();
export const videoInputs = {...commonInputs, index:outputIndex.describe("Zero-based MP4 index (default 0)")};
export const audioInputs = {...commonInputs, index:outputIndex.describe("Zero-based audio file index (default 0)")};
export const videoSchema = z.object(videoInputs);
export const audioSchema = z.object(audioInputs);
const mediaOutput = {
  prompt_id:z.uuid(), index:z.number().int().nonnegative(),
  delivery:z.enum(["player","starting","link","inline","chunk"]),
  filename:z.string().optional(), mimeType:z.string().optional(), url:z.url().optional(),
  size:z.number().int().positive().optional(),
  byte_offset:z.number().int().nonnegative().optional(), byte_length:z.number().int().positive().optional(),
  next_offset:z.number().int().nonnegative().nullable().optional(),
};
export const audioOutput = {...mediaOutput, audio_count:z.number().int().positive().optional()};
export const videoOutput = {...mediaOutput, video_count:z.number().int().positive().optional()};
// A bounded read for the audio widget. Its payload goes only to the app.
export const audioPartInputs = {prompt_id:commonInputs.prompt_id, index:outputIndex,
  byte_offset:commonInputs.byte_offset.unwrap()};
export const AUDIO_PART_BYTES = 128 * 1024;
export const videoPartInputs = audioPartInputs;
export const VIDEO_PART_BYTES = 512 * 1024;
type Kind = "audio" | "video";
type Format = {mime: string; min: number; matches: (b: Uint8Array) => boolean};
const tag = (b: Uint8Array, at: number, value: string) =>
  value.split("").every((c, i) => b[at+i] === c.charCodeAt(0));
const ogg = (b: Uint8Array) => tag(b, 0, "OggS");
const formats: Record<Kind, ReadonlyMap<string, Format>> = {
  video: new Map([["mp4", {mime:"video/mp4", min:8, matches:b => tag(b, 4, "ftyp")}]]),
  audio: new Map<string, Format>([
    ["flac", {mime:"audio/flac", min:4, matches:b => tag(b, 0, "fLaC")}],
    ["wav", {mime:"audio/wav", min:12, matches:b =>
      (tag(b, 0, "RIFF") || tag(b, 0, "RF64")) && tag(b, 8, "WAVE")}],
    ["mp3", {mime:"audio/mpeg", min:3, matches:b => tag(b, 0, "ID3") ||
      (b[0] === 0xff && (b[1] & 0xe0) === 0xe0 && (b[1] & 0x06) === 0x02)}],
    ["opus", {mime:"audio/ogg; codecs=opus", min:4, matches:ogg}],
    ["ogg", {mime:"audio/ogg", min:4, matches:ogg}],
  ]),
};
type Send = (path: string, init: RequestInit) => Promise<Response>;
type Output = { filename: string; subfolder: string; format: Format };
const fail = (text: string): CallToolResult => ({isError:true, content:[{type:"text",text}]});

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function safeSegment(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 255 &&
    value !== "." && value !== ".." && !/[/\\:\x00-\x1f\x7f]/.test(value);
}
export function outputsOfKind(outputs: unknown, kind: Kind): Output[] {
  const result: Output[] = [];
  if (!record(outputs)) return result;
  for (const node of Object.values(outputs)) {
    if (!record(node)) continue;
    for (const list of Object.values(node)) {
      if (!Array.isArray(list)) continue;
      for (const item of list) {
        if (!record(item) || item.type !== "output" || !safeSegment(item.filename)) continue;
        const extension = /\.([a-z0-9]+)$/i.exec(item.filename)?.[1].toLowerCase();
        const format = extension && formats[kind].get(extension);
        if (!format) continue;
        const subfolder = item.subfolder ?? "";
        if (typeof subfolder !== "string" || subfolder.length > 1024 ||
            (subfolder && !subfolder.split("/").every(safeSegment))) continue;
        result.push({filename:item.filename, subfolder, format});
      }
    }
  }
  return result;
}

export function base64(bytes: Uint8Array): string {
  const parts = [];
  // A multiple of three keeps padding confined to the very last block.
  for (let i=0; i<bytes.length; i+=24576)
    parts.push(btoa(String.fromCharCode(...bytes.subarray(i,i+24576))));
  return parts.join("");
}

export async function getMedia(kind: Kind, send: Send, publicOrigin: string,
  {prompt_id, index = 0, byte_offset, chunk_bytes}: z.infer<typeof videoSchema>,
  forPlayer = false): Promise<CallToolResult> {
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
  const files = outputsOfKind(job.outputs, kind);
  if (!files[index]) return fail("This job has " + files.length + " " + kind + " output(s); no file at index " + index + ".");
  const {filename, subfolder, format} = files[index];
  const path = "/view?" + new URLSearchParams({type:"output", filename, subfolder});
  const url = new URL(path, publicOrigin).href;
  const chunks = byte_offset !== undefined || chunk_bytes !== undefined;
  // Never put media bytes or base64 in a normal chat turn. Check just
  // the file signature and total size; the iframe loads authenticated media.
  const player = !chunks;
  const offset = byte_offset ?? 0, length = player ? 16 : chunk_bytes ?? MAX_PART;
  const file = await get(path, {Range:`bytes=${offset}-${offset + length - 1}`});
  const metadata = {prompt_id, filename, index, [kind + "_count"]:files.length, mimeType:format.mime, url};
  if (file.status !== 200 && file.status !== 206) {
    await file.body?.cancel();
    return fail(file.status === 416 ? "Byte offset is outside this file." :
       "Cannot retrieve the saved file (HTTP " + file.status + ").");
  }
  let total: number | undefined, expected: number | undefined;
  if (file.status === 206) {
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(file.headers.get("Content-Range") || "");
    if (!match || !Number.isSafeInteger(total = Number(match[3])) || Number(match[1]) !== offset ||
        total <= offset || Number(match[2]) !== Math.min(offset + length, total) - 1) {
      await file.body?.cancel();
      return fail("Invalid upstream byte range.");
    }
    expected = Math.min(length, total - offset);
  } else if (offset > 0) {
    await file.body?.cancel();
    return fail("Upstream ignored the byte offset. Open the authenticated file URL: " + url);
  }
  const data = await readAtMost(file, player && file.status === 200 ? MAX_PART : length);
  if (!data) {
    if (player) return {structuredContent:{...metadata,delivery:"link"},
      content:[{type:"text",text:"Open the signed-in " + kind + " player: " + url}]};
    return fail("Upstream did not honor the byte range. Open the authenticated file URL: " + url);
  }
  if (!data.length || (expected !== undefined && expected !== data.length))
    return fail("Incomplete media response.");
  total ??= data.length;
  if (total < format.min || (offset === 0 && data.length >= format.min && !format.matches(data)))
    return fail("File content does not match the expected format: " + format.mime + ".");
  // A Range-ignoring video cannot be read privately beyond one player chunk.
  if (player && kind === "video" && file.status === 200 && total > VIDEO_PART_BYTES)
    return {structuredContent:{...metadata,delivery:"link",size:total},
      content:[{type:"text",text:filename + " — use the video player. It streams the authenticated file URL: " + url}]};
  if (player) return {structuredContent:{...metadata,delivery:"player",size:total},
    content:[{type:"text",text:filename + " — use the " + kind + " player. The player loads authenticated media. " +
      "Retrieval success alone does not confirm that the player is visible. Give this link only if playback is unavailable or opening/saving is requested (website login required): " + url}]};
  const complete = offset === 0 && data.length === total;
  const next = offset + data.length < total ? offset + data.length : null;
  const info = {...metadata, delivery:complete ? "inline" : "chunk", size:total,
    byte_offset:offset, byte_length:data.length, next_offset:next};
  const blob = base64(data);
  if (forPlayer) return {structuredContent:info, content:[], _meta:{["split/" + kind + "Part"]:blob}};
  return {structuredContent:info, content:[
    {type:"text", text:JSON.stringify(info)},
    complete && kind === "audio" ? {type:"audio", mimeType:format.mime, data:blob} :
    {type:"resource", annotations:{audience:["user"]}, resource:{
      uri:complete ? url : url + `#bytes=${offset}-${offset + data.length - 1}`,
      mimeType:complete ? format.mime : "application/octet-stream", blob,
    }},
  ]};
}
