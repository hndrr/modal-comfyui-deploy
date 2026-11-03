import assert from "node:assert/strict";
import {test} from "node:test";
import {audioDocument} from "../src/audio-document.ts";

const path="/view?type=output&filename=take.mp3&subfolder=music&utm_source=chatgpt.com";
const navigation={"Sec-Fetch-Site":"cross-site","Sec-Fetch-Mode":"navigate","Sec-Fetch-Dest":"document"};
const get=(path,headers=navigation,options={})=>audioDocument(new Request("https://split.example"+path,{headers,...options}));
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
test("document cannot interpret a filename as HTML",async()=>{
  const r=get(path.replace("take.mp3","%22%3E%3Cscript%3E.mp3"));
  const html=await r.text();
  assert.ok(!html.includes("<script>") && html.includes("&lt;script&gt;"));
});
