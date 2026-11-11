import assert from "node:assert/strict";
import {test} from "node:test";
import {mediaDocument} from "../src/media-document.ts";

const path="/view?type=output&filename=take.mp3&subfolder=music&utm_source=chatgpt.com";
const navigation={"Sec-Fetch-Site":"cross-site","Sec-Fetch-Mode":"navigate","Sec-Fetch-Dest":"document"};
const get=(path,headers=navigation,options={})=>mediaDocument(new Request("https://split.example"+path,{headers,...options}));
test("an external audio navigation renders a private same-origin player; the media request stays raw",async()=>{
  for(const ext of ["mp3","wav","flac","opus","ogg"]){
    const r=get(path.replace(".mp3","."+ext)), html=await r.text();
    assert.equal(r.status,200);
    assert.match(r.headers.get("Cache-Control"),/no-store/);
    assert.match(r.headers.get("Content-Security-Policy"),/media-src 'self'/);
    assert.match(html,/<audio controls[^>]*src="\/api\/view\?type=output&amp;filename=/);
    assert.ok(html.includes("take."+ext) && !html.includes("utm_source"));
  }
  for(const headers of [{}, {...navigation,"Sec-Fetch-Dest":"audio"},{...navigation,Range:"bytes=0-10"}])
    assert.equal(get(path,headers),null);
  for(const bad of ["/api"+path,path.replace("output","input"),path.replace("take.mp3","a.html"),
    path.replace("music","..%2Fmodels"),path+"&preview=webp"])
    assert.equal(get(bad),null);
  assert.equal(get(path,navigation,{method:"HEAD"}),null);
});
test("MP4 navigation gets a signed-in player; its media and Range requests still get bytes",async()=>{
  const mp4=path.replace("take.mp3","take.MP4");
  const html=await get(mp4).text();
  assert.match(html,/<video controls playsinline[^>]+src="\/api\/view\?type=output&amp;filename=take\.MP4/);
  assert.match(html,/download="take.MP4"/);
  for(const [url,headers] of [["/api"+mp4,navigation],[mp4,{...navigation,"Sec-Fetch-Dest":"video"}],
    [mp4,{...navigation,Range:"bytes=0-"}]])assert.equal(get(url,headers),null);
});
test("document cannot interpret a filename as HTML",async()=>{
  const r=get(path.replace("take.mp3","%22%3E%3Cscript%3E.mp3"));
  const html=await r.text();
  assert.ok(!html.includes("<script>") && html.includes("&lt;script&gt;"));
});

test("3D documents embed the renderer; API, Range and temporary requests remain files",async()=>{
  for(const ext of ["glb","SPZ"]){
    const url="/view?type=output&filename=model."+ext+"&subfolder=3d/ComfyUI";
    const response=get(url),html=await response.text();
    assert.match(html,/id="viewport"/);
    assert.match(html,/ブラウザで開く/);
    assert.ok(html.includes('"filename":"model.'+ext+'"'));
    assert.match(response.headers.get("Content-Security-Policy"),/worker-src blob:/);
    assert.match(response.headers.get("Content-Security-Policy"),/frame-ancestors 'none'/);
    for(const [path,headers] of [["/api"+url,navigation],[url,{}],[url,{...navigation,Range:"bytes=0-"}],
      [url.replace("output","temp"),navigation],[url.replace("3d/ComfyUI",".split-temp/run"),navigation]])
      assert.equal(get(path,headers),null);
  }
  const injected=await get("/view?type=output&filename=%3C%2Fscript%3E.glb");
  assert.equal(injected,null);
  const encoded=await get("/view?type=output&filename=%3Cimg%3E.glb").text();
  assert.ok(encoded.includes('\\u003cimg>') && !encoded.includes('<img>.glb'));
});
