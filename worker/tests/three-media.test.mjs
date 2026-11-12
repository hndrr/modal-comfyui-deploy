import assert from "node:assert/strict";
import {test} from "node:test";
import {gzipSync, zstdCompressSync} from "node:zlib";
import {z} from "zod";
import {callSavedMedia} from "../src/saved-media.ts";
import {get3D, threeInputs, threePartInputs, threeOutput, THREE_PART_BYTES, THREE_INLINE_LIMIT} from "../src/three-media.ts";

const origin = "https://split.example.com";
const args = {filename:"立体 #1.GLB", subfolder:"render/テスト"};
const etag = '"version-1"';
function glb(binaryBytes = 0) {
  const json = JSON.stringify({asset:{version:"2.0"}, ...(binaryBytes ? {buffers:[{byteLength:binaryBytes}]} : {})});
  const padded = json.padEnd(Math.ceil(json.length / 4) * 4, " ");
  const bytes = Buffer.alloc(20 + padded.length + (binaryBytes ? 8 + binaryBytes : 0));
  bytes.write("glTF"); bytes.writeUInt32LE(2, 4); bytes.writeUInt32LE(bytes.length, 8);
  bytes.writeUInt32LE(padded.length, 12); bytes.write("JSON", 16); bytes.write(padded, 20);
  if (binaryBytes) {
    bytes.writeUInt32LE(binaryBytes, 20 + padded.length); bytes.write("BIN\0", 24 + padded.length);
    bytes.fill(0x69, 28 + padded.length);
  }
  return bytes;
}
function spz(version = 3) {
  const legacy = version < 4, header = Buffer.alloc(legacy ? 16 : 32);
  header.write("NGSP"); header.writeUInt32LE(version, 4); header.writeUInt32LE(1, 8); header[13] = 12;
  const streams = [version === 1 ? 6 : 9, 1, 3, 3, version < 3 ? 3 : 4].map(n => Buffer.alloc(n));
  if (legacy) return gzipSync(Buffer.concat([header, ...streams]));
  header[15] = streams.length; header.writeUInt32LE(32, 16);
  const toc = Buffer.alloc(streams.length * 16), compressed = streams.map(b => zstdCompressSync(b));
  streams.forEach((s, i) => {
    toc.writeBigUInt64LE(BigInt(compressed[i].length), i * 16);
    toc.writeBigUInt64LE(BigInt(s.length), i * 16 + 8);
  });
  return Buffer.concat([header, toc, ...compressed]);
}
function upstream(serve, expected = args) {
  const calls = [];
  const send = async (path, init) => {
    const url = new URL(path, origin);
    assert.equal(url.pathname, "/view"); assert.equal(url.origin, origin);
    assert.deepEqual(Object.fromEntries(url.searchParams), {type:"output", subfolder:"", ...expected});
    assert.equal(init.method, "GET"); assert.ok(init.signal instanceof AbortSignal);
    const range = new Headers(init.headers).get("Range");
    assert.match(range, /^bytes=\d+-\d+$/); calls.push(range);
    return serve(range, calls.length, new Headers(init.headers));
  };
  return {send, calls};
}
function ranged(bytes) {
  return range => {
    const [, a, b] = /^bytes=(\d+)-(\d+)$/.exec(range), start = Number(a), end = Math.min(Number(b), bytes.length - 1);
    return new Response(bytes.subarray(start, end + 1), {status:206, headers:{
      "Content-Range":`bytes ${start}-${end}/${bytes.length}`, "Content-Length":String(end - start + 1), ETag:etag}});
  };
}
function valid(result) {
  assert.ok(!result.isError, JSON.stringify(result));
  assert.ok(z.object(threeOutput).safeParse(result.structuredContent).success);
  return result.structuredContent;
}
function failed(result, pattern) {
  assert.equal(result.isError, true, JSON.stringify(result));
  assert.equal(result.structuredContent, undefined); assert.equal(result._meta, undefined);
  assert.deepEqual(result.content.map(c => c.type), ["text"]);
  if (pattern) assert.match(result.content[0].text, pattern);
}

test("GLB retrieval exposes only metadata and an encoded public output URL", async () => {
  const bytes = glb(), {send, calls} = upstream(ranged(bytes));
  const result = await get3D(send, origin, {...args, byte_offset:0, chunk_bytes:1});
  assert.deepEqual(valid(result), {...args, format:"glb", mimeType:"model/gltf-binary", size:bytes.length,
    delivery:"viewer", etag, url:origin + "/view?" + new URLSearchParams({type:"output", ...args})});
  assert.deepEqual(calls, ["bytes=0-0"]);
  assert.deepEqual(result.content.map(c => c.type), ["text"]); assert.equal(result._meta, undefined);
  assert.ok(JSON.stringify(result).length < 2048);
  assert.ok(!JSON.stringify(result).includes(bytes.toString("base64")));
});

test("SPZ versions are delivered as saved files without reparsing their contents", async () => {
  for (const version of [1, 2, 3, 4]) {
    const bytes = spz(version), input = {filename:"scene.spz"};
    const result = await get3D(upstream(ranged(bytes), input).send, origin, input);
    const metadata = valid(result);
    assert.equal(metadata.format, "spz"); assert.equal(metadata.subfolder, "");
    assert.equal(metadata.mimeType, "application/octet-stream"); assert.equal(metadata.size, bytes.length);
    assert.equal(metadata.delivery, "viewer"); assert.equal(result._meta, undefined);
  }
});

test("path schemas reject traversal, hidden components, controls and unsafe byte offsets before any fetch", async () => {
  const send = () => assert.fail("Invalid input must not fetch");
  for (const filename of ["", ".scene.glb", "..", "../a.glb", "/a.glb", "a/b.glb", "a\\b.glb",
      "C:a.glb", "a\0.glb", "a\n.glb", "a\x7f.glb", "a".repeat(256), 1, null]) {
    const input = {...args, filename};
    assert.equal(z.object(threeInputs).safeParse(input).success, false);
    failed(await get3D(send, origin, input), /Invalid/);
  }
  for (const subfolder of ["../render", "render/../other", ".private", "render/.cache", "/render", "render/",
      "a//b", "a\\b", "C:render", "a\n", "a".repeat(256), Array(5).fill("a".repeat(205)).join("/"), null]) {
    const input = {...args, subfolder};
    assert.equal(z.object(threeInputs).safeParse(input).success, false);
    failed(await get3D(send, origin, input), /Invalid/);
  }
  for (const byte_offset of [undefined, -1, 0.5, NaN, Infinity, "0", Number.MAX_SAFE_INTEGER - THREE_PART_BYTES + 1]) {
    const input = {...args, byte_offset};
    assert.equal(z.object(threePartInputs).safeParse(input).success, false);
    failed(await get3D(send, origin, input, true), /Invalid/);
  }
  const longest = ["a".repeat(255), "b".repeat(255), "c".repeat(255), "d".repeat(254), "e"].join("/");
  assert.equal(longest.length, 1024);
  assert.ok(z.object(threeInputs).safeParse({...args, subfolder:longest}).success);
  assert.equal(z.object(threeInputs).safeParse({...args, subfolder:longest + "e"}).success, false);
  assert.ok(z.object(threePartInputs).safeParse({...args, byte_offset:Number.MAX_SAFE_INTEGER - THREE_PART_BYTES}).success);
  for (const filename of ["a.ply", "a.obj", "a.gltf", "glb"])
    failed(await get3D(send, origin, {filename}), /GLB and SPZ/);
});

test("viewer limit is inclusive and larger files return a link after only a one-byte read", async () => {
  assert.equal(THREE_INLINE_LIMIT, 128 * 1024 * 1024);
  for (const size of [THREE_INLINE_LIMIT, THREE_INLINE_LIMIT + 4]) {
    const prefix = Buffer.of(0x67);
    const {send, calls} = upstream(() => new Response(prefix, {status:206, headers:{"Content-Range":`bytes 0-0/${size}`,ETag:etag}}));
    const result = await get3D(send, origin, args), metadata = valid(result);
    assert.equal(metadata.delivery, size <= THREE_INLINE_LIMIT ? "viewer" : "link");
    assert.equal(metadata.size, size); assert.equal(result._meta, undefined);
    assert.deepEqual(calls, ["bytes=0-0"]); assert.ok(JSON.stringify(result).length < 2048);
  }
});

test("private 512 KiB parts reconstruct GLB/SPZ with no model-visible bytes", async () => {
  assert.equal(THREE_PART_BYTES, 512 * 1024);
  for (const [filename, bytes] of [["scene.glb", glb(THREE_PART_BYTES)], ["scene.spz", spz()]]) {
    const input = {filename}, {send, calls} = upstream(ranged(bytes), input), parts = [];
    let offset = 0;
    do {
      const result = await get3D(send, origin, {...input, byte_offset:offset, etag}, true), info = valid(result);
      assert.equal(info.delivery, "chunk"); assert.equal(info.byte_offset, offset);
      assert.equal(info.byte_length, Math.min(THREE_PART_BYTES, bytes.length - offset));
      assert.equal(calls.at(-1), `bytes=${offset}-${offset + THREE_PART_BYTES - 1}`);
      assert.deepEqual(result.content, []); assert.deepEqual(Object.keys(result._meta), ["split/3dPart"]);
      const blob = result._meta["split/3dPart"];
      assert.ok(!JSON.stringify(result.structuredContent).includes(blob));
      parts.push(Buffer.from(blob, "base64")); offset = info.next_offset;
    } while (offset !== null);
    assert.deepEqual(Buffer.concat(parts), bytes);
    assert.equal(calls.length, parts.length, "Each part makes exactly one upstream request");
  }
});

test("one ETag pins every part, rejecting same-size replacements even at offset zero", async () => {
  const bytes=glb(THREE_PART_BYTES),replacement=Buffer.from(bytes);replacement[replacement.length-1]^=1;
  let current=bytes,version=etag;
  const {send}=upstream((range,_call,headers)=>{
    const ifRange=headers.get("If-Range");
    if(ifRange && ifRange!==version)
      return new Response(current,{headers:{ETag:version,"Content-Length":String(current.length)}});
    const response=ranged(current)(range);response.headers.set("ETag",version);return response;
  });
  const info=valid(await get3D(send,origin,args));assert.equal(info.etag,etag);
  const first=valid(await get3D(send,origin,{...args,byte_offset:0,etag:info.etag},true));
  current=replacement;version='"version-2"';
  failed(await get3D(send,origin,{...args,byte_offset:first.next_offset,etag:info.etag},true),/version changed/);
  for(const honorIfRange of [true,false]){
    const swapped=upstream((range,call,headers)=>{
      assert.equal(headers.get("If-Range"),etag);
      const response=honorIfRange?new Response(replacement):ranged(replacement)(range);
      response.headers.set("ETag",'"version-2"');return response;
    });
    failed(await get3D(swapped.send,origin,{...args,byte_offset:0,etag},true),/version changed/);
    assert.equal(swapped.calls.length,1);
  }
});

test("unverifiable versions fall back to browser links and continuation requires a strong ETag",async()=>{
  for(const tag of [null,'W/"weak"']){
    const {send}=upstream(range=>{
      const response=ranged(glb())(range);
      if(tag)response.headers.set("ETag",tag);else response.headers.delete("ETag");return response;
    });
    const result=await get3D(send,origin,args);
    assert.equal(valid(result).delivery,"link");assert.equal(result.structuredContent.etag,undefined);
    failed(await get3D(send,origin,{...args,byte_offset:0},true),/ETag/);
  }
  const noFetch=()=>assert.fail("Invalid version must not fetch");
  failed(await get3D(noFetch,origin,{...args,byte_offset:1},true),/etag/);
  for(const etag of ['W/"weak"','not-quoted','"unsafe\r\n"','"'+'x'.repeat(512)+'"'])
    failed(await get3D(noFetch,origin,{...args,byte_offset:0,etag},true),/Invalid/);
});

test("malformed, inconsistent and truncated ranges are rejected for both probes and parts", async () => {
  const bytes = glb(THREE_PART_BYTES);
  for (const part of [false, true]) {
    for (const kind of ["missing", "wildcard", "unsafe", "wrong-start", "wrong-end", "short-end", "truncated", "oversized",
        "wrong-length", "invalid-length", "encoded"]) {
      const {send} = upstream((range, call) => {
        const offset = part ? 1 : 0, length = part ? THREE_PART_BYTES : 1, end = offset + length - 1;
        const headers = {"Content-Range":`bytes ${offset}-${end}/${bytes.length}`,ETag:etag};
        let body = bytes.subarray(offset, end + 1);
        if (kind === "missing") delete headers["Content-Range"];
        if (kind === "wildcard") headers["Content-Range"] = `bytes ${offset}-${end}/*`;
        if (kind === "unsafe") headers["Content-Range"] = `bytes ${offset}-${end}/9007199254740992`;
        if (kind === "wrong-start") headers["Content-Range"] = `bytes ${offset + 1}-${end}/${bytes.length}`;
        if (kind === "wrong-end") headers["Content-Range"] = `bytes ${offset}-${end + 1}/${bytes.length}`;
        if (kind === "short-end") headers["Content-Range"] = `bytes ${offset}-${end - 1}/${bytes.length}`;
        if (kind === "truncated") body = body.subarray(0, -1);
        if (kind === "oversized") body = Buffer.concat([body, Buffer.of(0)]);
        if (kind === "wrong-length") headers["Content-Length"] = String(length - 1);
        if (kind === "invalid-length") headers["Content-Length"] = "1e3";
        if (kind === "encoded") headers["Content-Encoding"] = "gzip";
        return new Response(body, {status:206, headers});
      });
      failed(await get3D(send, origin, {...args, byte_offset:1, etag}, part));
    }
  }
});

test("ignored ranges, incomplete lengths and out-of-range offsets fail", async () => {
  const bytes = glb();
  failed(await get3D(upstream(() => new Response(bytes)).send, origin, args), /exceeds/);
  failed(await get3D(upstream(() => new Response(bytes)).send, origin, {...args, byte_offset:1, etag}, true), /version changed/);
  failed(await get3D(upstream(() => new Response(null,{status:416})).send, origin, {...args, byte_offset:bytes.length, etag}, true), /outside/);
  for (const headers of [{"Content-Length":String(bytes.length - 1)}, {"Content-Range":`bytes 0-${bytes.length - 1}/${bytes.length}`}])
    failed(await get3D(upstream(() => new Response(bytes, {headers})).send, origin, args));
});

test("unbounded and oversized upstream bodies are cancelled within the read budget", async () => {
  for (const advertised of [true, false]) {
    let cancelled = false, pulls = 0;
    const body = new ReadableStream({pull(c) { pulls++; c.enqueue(new Uint8Array(2048)); }, cancel() { cancelled = true; }});
    const {send} = upstream(() => new Response(body, {headers:advertised ? {"Content-Length":String(THREE_INLINE_LIMIT + 1)} : {}}));
    failed(await get3D(send, origin, args)); assert.equal(cancelled, true);
    assert.ok(pulls <= 4, `Read ${pulls * 2048} bytes`);
  }
});

test("deletion, HTTP failures, redirects and transport errors never submit generation or expose error bodies", async () => {
  for (const status of [404, 410, 416, 302, 403, 500]) {
    for (const part of [false, true]) {
      let cancelled = false;
      const {send, calls} = upstream((range, call) => {
        return new Response(new ReadableStream({cancel() { cancelled = true; }}), {status});
      });
      const result = await get3D(send, origin, {...args, byte_offset:0}, part);
      failed(result, status === 416 ? /outside/ : new RegExp(`HTTP ${status}`));
      assert.equal(cancelled, true); assert.equal(calls.length, 1);
      if ([404, 410].includes(status)) assert.match(result.content[0].text, /No generation was submitted/);
    }
  }
  for (const serve of [() => { throw Error("private upstream credential"); },
    () => new Response(new ReadableStream({start(c) { c.error(Error("private upstream body")); }}))]) {
    const {send, calls} = upstream(serve);
    const result = await callSavedMedia(send, "get_3d", args, origin);
    failed(result, /no generation was submitted/); assert.equal(calls.length, 1);
    assert.ok(!JSON.stringify(result).includes("private upstream"));
  }
});
