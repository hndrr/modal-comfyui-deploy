import {createViewer} from "./render.ts";
import {readAtMost} from "../src/bounded-response.ts";

type Info = {filename: string; subfolder: string; format: "glb" | "spz"; size: number;
  url: string; delivery: "viewer" | "link"; etag?: string};
type ToolResult = {isError?: boolean; structuredContent?: unknown; content?: {type: string; text?: string}[];
  _meta?: Record<string, unknown>};
declare global {
  interface Window {
    split3D: {origin: string; file?: {filename: string; subfolder: string}};
    openai?: {callTool?: (name: string, args: unknown) => Promise<ToolResult>;
      toolOutput?: unknown; toolResponseMetadata?: Record<string, ToolResult>};
  }
}
const PART = 512 * 1024, LIMIT = 128 * 1024 * 1024;
const config = window.split3D, browser = !!config.file;
const viewport = document.querySelector<HTMLElement>("#viewport")!;
const status = document.querySelector<HTMLElement>("#status")!;
const name = document.querySelector<HTMLElement>("#name")!;
const reset = document.querySelector<HTMLButtonElement>("#reset")!;
const retry = document.querySelector<HTMLButtonElement>("#retry")!;
const link = document.querySelector<HTMLAnchorElement>("#open")!;
const save = document.querySelector<HTMLAnchorElement>("#save")!;
let peer = "*", initialized = false, serverTools = false, openLinks = false, sequence = 1;
let current: Info | undefined, version = 0, viewer: ReturnType<typeof createViewer> | undefined;
let download: AbortController | undefined, objectUrl: string | undefined;
let savedBytes: ArrayBuffer | undefined;
const pending = new Map<number, {resolve: (result: ToolResult) => void; reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>}>();
function send(message: object) { parent.postMessage({jsonrpc: "2.0", ...message}, peer); }
function request(name: string, args: object): Promise<ToolResult> {
  if (!serverTools && window.openai?.callTool) return window.openai.callTool(name, args);
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(Error("応答がありません。再取得してください。")); }, 65000);
    pending.set(id, {resolve, reject, timer});
    send({id, method: "tools/call", params: {name, arguments: args}});
  });
}
function clear() {
  version++;
  download?.abort(); download = undefined;
  viewer?.dispose(); viewer = undefined;
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = undefined; savedBytes = undefined;
  save.hidden = true; save.removeAttribute("href"); reset.disabled = true; retry.hidden = true;
  viewport.dataset.state = "empty";
}
function fail(error: unknown) {
  viewer?.dispose(); viewer = undefined; reset.disabled = true;
  viewport.dataset.state = "error";
  status.textContent = (error instanceof Error ? error.message : "3Dを表示できませんでした。") +
    (!current ? " get_3dをもう一度実行してください。" : browser ?
      " 元のファイルを保存して確認できます。" : " ブラウザで開いて確認できます。");
  retry.hidden = !current;
}
function readInfo(value: unknown): Info | undefined {
  if (!value || typeof value !== "object") return;
  const info = value as Info;
  if (typeof info.filename !== "string" || typeof info.subfolder !== "string" ||
      !["glb", "spz"].includes(info.format) || !Number.isSafeInteger(info.size) || info.size <= 0 ||
      !["viewer", "link"].includes(info.delivery)) return;
  try {
    const url = new URL(info.url);
    if (url.origin === config.origin && url.pathname === "/view" && !url.username && !url.password)
      return info;
  } catch { /* A malformed tool result has no usable viewer link. */ }
}
function setLinks(info: Info) {
  link.href = info.url; link.hidden = browser;
  link.textContent = info.size > LIMIT ? "ブラウザで元のファイルを保存" : "ブラウザで開く";
  const raw = new URL(info.url); raw.pathname = "/api/view";
  // Browser downloads use the existing Access session. Inline downloads are
  // offered once the MCP-authenticated bytes have been assembled into a Blob.
  if (browser) { save.href = raw.href; save.download = info.filename; save.hidden = false; }
}
async function load() {
  const info = current;
  if (!info) return;
  clear(); setLinks(info);
  const run = version;
  if (!browser && (info.size > LIMIT || info.delivery === "link")) {
    status.textContent = info.size > LIMIT ? "128 MiBを超えるモデルは自動表示の対象外です。ブラウザで元のファイルを保存してください。" :
      "このモデルはブラウザで開いてください。";
    return;
  }
  if (!browser && !initialized && !window.openai?.callTool) {
    status.textContent = "3Dモデルを読み込めます。"; retry.hidden = false; return;
  }
  if (!browser && initialized && !serverTools && !window.openai?.callTool) {
    status.textContent = "このホストでは3Dモデルを取得できません。ブラウザで開いてください。"; return;
  }
  viewport.dataset.state = "loading";
  status.textContent = "3Dモデルを取得中…";
  try {
    let buffer: ArrayBuffer;
    if (browser) {
      const raw = new URL(info.url); raw.pathname = "/api/view";
      download = new AbortController();
      const response = await fetch(raw, {signal: download.signal, credentials: "same-origin"});
      if (!response.ok) throw Error(`ファイルを取得できませんでした（HTTP ${response.status}）。`);
      const bytes = await readAtMost(response, LIMIT);
      if (!bytes) {
        viewport.dataset.state = "empty";
        status.textContent = "128 MiBを超えるため自動表示を停止しました。元のファイルを保存してください。";
        return;
      }
      buffer = bytes.buffer as ArrayBuffer;
    } else {
      const bytes = new Uint8Array(info.size);
      let offset = 0;
      while (offset < info.size) {
        const result = await request("read_3d_part", {filename: info.filename, subfolder: info.subfolder, byte_offset: offset, etag: info.etag});
        if (run !== version) return;
        if (result.isError) throw Error(result.content?.find(item => item.type === "text")?.text || "3Dファイルを取得できませんでした。");
        const meta = result.structuredContent as Record<string, unknown> | undefined;
        const data = result._meta?.["split/3dPart"];
        if (!meta || meta.filename !== info.filename || meta.subfolder !== info.subfolder || meta.size !== info.size ||
            meta.format !== info.format || meta.etag !== info.etag || meta.byte_offset !== offset || typeof data !== "string" ||
            data.length > 4 * Math.ceil(PART / 3)) throw Error("指定した3Dファイルの応答ではありません。");
        const chunk = Uint8Array.from(atob(data), c => c.charCodeAt(0)), next = offset + chunk.length;
        if (!chunk.length || chunk.length !== meta.byte_length || next > info.size ||
            meta.next_offset !== (next < info.size ? next : null)) throw Error("3Dファイルが不完全です。");
        bytes.set(chunk, offset); offset = next;
        status.textContent = `3Dモデルを取得中… ${Math.round(offset / info.size * 100)}%`;
      }
      buffer = bytes.buffer;
      savedBytes = buffer;
      save.href = info.url; save.download = info.filename; save.hidden = false;
    }
    if (run !== version) return;
    status.textContent = "3Dモデルを読み込み中…";
    viewer = createViewer(viewport, error => { if (run === version) fail(error); });
    await viewer.load(buffer, info.format);
    if (run !== version || !viewer) return;
    reset.disabled = false; viewport.dataset.state = "ready";
    status.textContent = "ドラッグで回転 · スクロールで拡大 · 右ドラッグで移動";
  } catch (error) { if (run === version) fail(error); }
}
function render(result: ToolResult) {
  if (result.isError) { clear(); current = undefined; link.hidden = true; fail(Error(result.content?.find(x => x.type === "text")?.text || "3Dファイルを取得できませんでした。")); return; }
  const info = readInfo(result.structuredContent);
  if (!info) return;
  if (current && current.url === info.url && current.size === info.size && current.etag === info.etag && current.delivery === info.delivery) return;
  current = info; name.textContent = info.filename;
  void load();
}
reset.onclick = () => viewer?.reset(); retry.onclick = () => { void load(); };
save.onclick = () => {
  if (!savedBytes || !current || objectUrl) return;
  objectUrl = URL.createObjectURL(new Blob([savedBytes], {
    type: current.format === "glb" ? "model/gltf-binary" : "application/octet-stream"}));
  save.href = objectUrl;
};
link.onclick = event => {
  if (openLinks) { event.preventDefault(); send({id: ++sequence, method: "ui/open-link", params: {url: link.href}}); }
};
function legacy(globals: Window["openai"]) {
  const meta = globals?.toolResponseMetadata;
  const result = meta?.mcp_tool_result || meta?.call_tool_result;
  if (result) render(result); else if (globals?.toolOutput) render({structuredContent: globals.toolOutput});
}
if (browser) {
  const file = config.file!;
  const url = new URL("/view?" + new URLSearchParams({type: "output", ...file}), config.origin).href;
  current = {...file, format: file.filename.toLowerCase().endsWith(".glb") ? "glb" : "spz", size: 1, url, delivery: "viewer"};
  name.textContent = file.filename;
  void load();
} else {
  addEventListener("message", event => {
    if (event.source !== parent || (peer !== "*" && event.origin !== peer)) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.id === 1 && message.result) {
      if (event.origin !== "null") peer = event.origin;
      initialized = true;
      serverTools = !!message.result.hostCapabilities?.serverTools;
      openLinks = !!message.result.hostCapabilities?.openLinks;
      send({method: "ui/notifications/initialized", params: {}});
      send({method: "ui/notifications/size-changed", params: {height: document.body.scrollHeight}});
      if (current && !viewer && retry.hidden === false) void load();
    } else if (pending.has(message.id) && ("result" in message || "error" in message)) {
      const call = pending.get(message.id)!; pending.delete(message.id); clearTimeout(call.timer);
      if (message.error) call.reject(Error(message.error.message || "3Dファイルを取得できませんでした。"));
      else call.resolve(message.result);
    } else if (message.method === "ui/notifications/tool-result") render(message.params);
    else if (message.method === "ui/resource-teardown") {
      clear(); current = undefined;
      for (const call of pending.values()) { clearTimeout(call.timer); call.reject(Error("表示を終了しました。")); }
      pending.clear(); send({id: message.id, result: {}});
    }
  });
  addEventListener("openai:set_globals", event => legacy((event as CustomEvent).detail?.globals));
  new ResizeObserver(() => {
    if (initialized) send({method: "ui/notifications/size-changed", params: {height: document.body.scrollHeight}});
  }).observe(document.body);
  send({id: 1, method: "ui/initialize", params: {protocolVersion: "2026-01-26",
    appInfo: {name: "split-3d", version: "1"}, appCapabilities: {}}});
  legacy(window.openai);
}
