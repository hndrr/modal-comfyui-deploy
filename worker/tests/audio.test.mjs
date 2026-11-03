import assert from "node:assert/strict";
import {test} from "node:test";
import {callSplit} from "../src/remote-mcp.ts";

const id = "fe08cba3-93d1-439f-b5c4-ccb6d9be3421", origin = "https://split.example.com";
const output = filename => ({filename, subfolder:"music/mix", type:"output"});
function saved(entries, contents, extra = {}, index = 0, tool = "get_audio") {
  return callSplit(async (path, init) => {
    if (path === "/split/startup") return Response.json({ready:true});
    if (path === "/history/"+id) return Response.json({[id]:{status:{status_str:"success",completed:true},
      outputs:{"1":{audio:entries}}}});
    const url = new URL(path, origin);
    assert.equal(url.pathname, "/view");
    assert.deepEqual(Object.fromEntries(url.searchParams), output(contents.name));
    return contents.read(init.headers.Range);
  }, tool, {prompt_id:id, index, ...extra}, origin);
}
const formats = [
  ["flac","audio/flac",Buffer.from("fLaCexample")],
  ["mp3","audio/mpeg",Buffer.from("ID3example")],
  ["wav","audio/wav",Buffer.from("RIFFxxxxWAVEexample")],
  ["ogg","audio/ogg",Buffer.from("OggSexample")],
  ["opus","audio/ogg; codecs=opus",Buffer.from("OggSexample")],
];
test("an explicit byte read returns native MCP audio for supported output formats", async () => {
  for (const [extension, mime, bytes] of formats) {
    const name = "音 #1."+extension;
    const entries = [output("also.mp4"), {...output("input.wav"),type:"input"},
      {...output("outside.mp3"),subfolder:"../input"}, output("other.mp3"), output(name)];
    const r = await saved(entries,{name, read:() => new Response(bytes)}, {byte_offset:0}, 1);
    assert.equal(r.structuredContent?.audio_count,2);
    assert.equal(r.content[1].type,"audio");
    assert.equal(r.content[1].mimeType,mime);
    assert.deepEqual(Buffer.from(r.content[1].data,"base64"),bytes);
    assert.equal(new URL(r.structuredContent.url).searchParams.get("filename"),name);
  }
});
test("chat gets a small player result and usable URL, not megabytes of encoded audio",async()=>{
  const size=2_500_000,name="track.mp3",header=Buffer.from("ID3"+"x".repeat(13));
  const r=await saved([output(name)],{name,read:range=>{
    assert.equal(range,"bytes=0-15");
    return new Response(header,{status:206,headers:{"Content-Range":"bytes 0-15/"+size}});
  }});
  assert.equal(r.structuredContent.delivery,"player");
  assert.equal(r.structuredContent.size,size);
  assert.deepEqual(r.content.map(x=>x.type),["text"]);
  assert.ok(r.content[0].text.includes(r.structuredContent.url));
  assert.ok(JSON.stringify(r).length<2048);
});
test("a cold CPU can render the initial player before the saved file is readable",async()=>{
  const r=await callSplit(async(path)=>{
    assert.equal(path,"/split/startup");
    return Response.json({ready:false,failed:false});
  },"get_audio",{prompt_id:id},origin);
  assert.deepEqual(r.structuredContent,{delivery:"starting",prompt_id:id,index:0});
  assert.ok(!r.isError);
});
test("large audio has a player URL; explicit byte ranges reconstruct the same file", async () => {
  const [extension, mime, bytes] = formats[0], name = "example."+extension, entries = [output(name)];
  const linked = await saved(entries,{name, read:() => new Response(Buffer.from("fLaC"+"x".repeat(12)),{status:206,
    headers:{"Content-Range":"bytes 0-15/8388608"}})});
  assert.equal(linked.structuredContent.delivery,"player");
  assert.equal(linked.structuredContent.mimeType,mime);
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

test("widget reads verified chunks without putting base64 in model-visible output",async()=>{
  const bytes=Buffer.alloc(512*1024+3);bytes.write("fLaC");const name="take.flac",parts=[];
  let offset=0;
  do {
    const r=await saved([output(name)],{name,read:range=>{
      const last=Math.min(offset+128*1024,bytes.length)-1;
      assert.equal(range,`bytes=${offset}-${offset+128*1024-1}`);
      return new Response(bytes.subarray(offset,last+1),{status:206,
        headers:{"Content-Range":`bytes ${offset}-${last}/${bytes.length}`}});
    }},{byte_offset:offset},0,"read_audio_part");
    assert.deepEqual(r.content,[]);
    assert.equal(r.structuredContent.mimeType,"audio/flac");
    parts.push(Buffer.from(r._meta["split/audioPart"],"base64"));
    offset=r.structuredContent.next_offset;
  } while(offset!==null);
  assert.deepEqual(Buffer.concat(parts),bytes);
});
