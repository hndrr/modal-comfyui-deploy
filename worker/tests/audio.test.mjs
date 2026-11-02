import assert from "node:assert/strict";
import {test} from "node:test";
import {callSplit} from "../src/remote-mcp.ts";

const id = "fe08cba3-93d1-439f-b5c4-ccb6d9be3421", origin = "https://split.example.com";
const output = filename => ({filename, subfolder:"music/mix", type:"output"});
function saved(entries, contents, extra = {}, index = 0) {
  return callSplit(async (path, init) => {
    if (path === "/split/startup") return Response.json({ready:true});
    if (path === "/history/"+id) return Response.json({[id]:{status:{status_str:"success",completed:true},
      outputs:{"1":{audio:entries}}}});
    const url = new URL(path, origin);
    assert.equal(url.pathname, "/view");
    assert.deepEqual(Object.fromEntries(url.searchParams), output(contents.name));
    return contents.read(init.headers.Range);
  }, "get_audio", {prompt_id:id, index, ...extra}, origin);
}
const formats = [
  ["flac","audio/flac",Buffer.from("fLaCexample")],
  ["mp3","audio/mpeg",Buffer.from("ID3example")],
  ["wav","audio/wav",Buffer.from("RIFFxxxxWAVEexample")],
  ["ogg","audio/ogg",Buffer.from("OggSexample")],
  ["opus","audio/ogg; codecs=opus",Buffer.from("OggSexample")],
];
test("returns native MCP audio with the original bytes for supported output formats", async () => {
  for (const [extension, mime, bytes] of formats) {
    const name = "音 #1."+extension;
    const entries = [output("also.mp4"), {...output("input.wav"),type:"input"},
      {...output("outside.mp3"),subfolder:"../input"}, output("other.mp3"), output(name)];
    const r = await saved(entries,{name, read:() => new Response(bytes)}, {}, 1);
    assert.equal(r.structuredContent?.audio_count,2);
    assert.equal(r.content[1].type,"audio");
    assert.equal(r.content[1].mimeType,mime);
    assert.deepEqual(Buffer.from(r.content[1].data,"base64"),bytes);
    assert.equal(new URL(r.structuredContent.url).searchParams.get("filename"),name);
  }
});
test("large audio has an authenticated link; byte ranges reconstruct the same file", async () => {
  const [extension, mime, bytes] = formats[0], name = "example."+extension, entries = [output(name)];
  const linked = await saved(entries,{name, read:() => new Response(null,{status:206,
    headers:{"Content-Range":"bytes 0-4194303/8388608"}})});
  assert.equal(linked.structuredContent.delivery,"link");
  assert.equal(linked.content[1].mimeType,mime);
  let offset=0;
  const parts=[];
  do {
    const res=await saved(entries,{name, read:range => {
      const end = Math.min(bytes.length - 1,offset + 4);
      assert.equal(range,`bytes=${offset}-${offset+4}`);
      return new Response(bytes.subarray(offset,end+1),{status:206,
        headers:{"Content-Range":`bytes ${offset}-${end}/${bytes.length}`}});
    }},{byte_offset:offset,chunk_bytes:5});
    assert.equal(res.content[1].resource.mimeType,"application/octet-stream");
    parts.push(Buffer.from(res.content[1].resource.blob,"base64"));
    offset=res.structuredContent.next_offset;
  } while (offset !== null);
  assert.deepEqual(Buffer.concat(parts),bytes);
});
test("does not treat a video track or arbitrary content as an audio file", async () => {
  const noAudio=await saved([output("only.mp4")],null);
  assert.match(noAudio.content[0].text,/0 audio output/);
  const invalid=await saved([output("fake.wav")],{name:"fake.wav",read:() => new Response("<html>wrong file</html>")});
  assert.match(invalid.content[0].text,/expected format/);
});
