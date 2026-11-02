import assert from "node:assert/strict";
import {test} from "node:test";
import {getVideo, videoSchema} from "../src/video.ts";
import {callSplit} from "../src/remote-mcp.ts";

const id = "afdb2c73-44e7-4c88-a01e-effbc8dbe762", origin = "https://split.example.com";
const src = {filename:"波 #1.mp4",subfolder:"render/test",type:"output"};
const mp4 = Buffer.concat([Buffer.from([0,0,0,20]),Buffer.from("ftypisom12345678")]);
function job(items) { return {[id]:{status:{status_str:"success",completed:true},outputs:{"15":{images:items}}}}; }
function upstream(history, serve) {
  return async (path,init) => {
    assert.equal(init.method,"GET");
    if (path === "/history/"+id) return Response.json(history);
    const url = new URL(path,origin);
    assert.equal(url.origin,origin); assert.equal(url.pathname,"/view");
    assert.deepEqual(Object.fromEntries(url.searchParams),src);
    return serve(init.headers.Range);
  };
}
const ranged = (range) => {
  const [,a,b]=/^bytes=(\d+)-(\d+)$/.exec(range);
  const end=Math.min(+b,mp4.length-1);
  return new Response(mp4.subarray(+a,end+1),{status:206,headers:{"Content-Range":`bytes ${a}-${end}/${mp4.length}`}});
};
test("completed job returns the original MP4, with a safely encoded browser URL", async () => {
  const items = [{...src,type:"input"},{...src,filename:"../other.mp4"},
    {...src,subfolder:"../inputs"},{...src,filename:"file.png"},src];
  const r=await getVideo(upstream(job(items),ranged),origin,{prompt_id:id});
  const {resource}=r.content[1];
  assert.equal(resource.mimeType,"video/mp4");
  assert.deepEqual(Buffer.from(resource.blob,"base64"),mp4);
  assert.equal(new URL(resource.uri).searchParams.get("filename"),src.filename);
  assert.equal(r.structuredContent.video_count,1);
  assert.equal(r.structuredContent.next_offset,null);
});
test("client can reconstruct a file from separate MCP calls", async () => {
  let offset=0;
  const chunks=[];
  do {
    const r=await getVideo(upstream(job([src]),ranged),origin,{prompt_id:id,byte_offset:offset,chunk_bytes:8});
    assert.equal(r.content[1].resource.mimeType,"application/octet-stream");
    assert.equal(r.structuredContent.byte_offset,offset);
    chunks.push(Buffer.from(r.content[1].resource.blob,"base64"));
    offset=r.structuredContent.next_offset;
  } while (offset!==null);
  assert.deepEqual(Buffer.concat(chunks),mp4);
});
test("large output produces a protected link without buffering the file",async()=>{
  let cancelled=false;
  const large=()=>new Response(new ReadableStream({cancel(){cancelled=true;}}),
    {status:206,headers:{"Content-Range":"bytes 0-4194303/9000000"}});
  const r=await getVideo(upstream(job([src]),large),origin,{prompt_id:id});
  assert.equal(r.content[1].type,"resource_link"); assert.equal(r.structuredContent.size,9000000);
  assert.equal(cancelled,true);
});
test("missing jobs, failed jobs and invalid output selections never fetch a file",async()=>{
  for (const [history,args] of [[{},{prompt_id:id}],[job([]),{prompt_id:id}],
      [job([src]),{prompt_id:id,index:1}],[{[id]:{status:{completed:false},outputs:job([src])[id].outputs}},{prompt_id:id}]]) {
    const send=async path=>{assert.equal(path,"/history/"+id);return Response.json(history);};
    assert.equal((await getVideo(send,origin,args)).isError,true);
  }
  assert.equal(videoSchema.safeParse({prompt_id:"../other"}).success,false);
  assert.equal(videoSchema.safeParse({prompt_id:id,byte_offset:-1}).success,false);
});
test("rejected redirects, wrong ranges, truncation and non-video responses are not returned as MP4",async()=>{
  for (const bad of [
    ()=>new Response(null,{status:302,headers:{Location:"https://elsewhere.example"}}),
    ()=>new Response(mp4,{status:206,headers:{"Content-Range":"bytes 1-20/21"}}),
    ()=>new Response(mp4.slice(0,12),{status:206,headers:{"Content-Range":"bytes 0-19/20"}}),
    ()=>new Response("<html>error</html>"),
  ]) assert.equal((await getVideo(upstream(job([src]),bad),origin,{prompt_id:id})).isError,true);
});
test("an initializing CPU cannot reach video history or submit a prompt",async()=>{
  const res=await callSplit(async(path)=>{assert.equal(path,"/split/startup");throw new DOMException("starting","TimeoutError");},
    "get_video",{prompt_id:id},origin);
  assert.match(res.content[0].text,/starting/i);
});
