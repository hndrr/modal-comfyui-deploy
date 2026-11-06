import assert from "node:assert/strict";
import {test} from "node:test";
import {runInNewContext} from "node:vm";
import {imageViewerHtml} from "../src/image-viewer.ts";

const result={structuredContent:{filename:"画像.png",mimeType:"image/png",delivery:"inline",
  url:"https://split.example.com/view?type=output&filename=%E7%94%BB%E5%83%8F.png"},
  content:[{type:"image",mimeType:"image/png",data:"cG5n"}]};

function viewer(openai){
  const handlers={},messages=[];
  const elements=Object.fromEntries(["img","span","a"].map(k=>[k,{hidden:true,removeAttribute(k){delete this[k];}}]));
  const parent={postMessage:m=>messages.push(m)};
  runInNewContext(imageViewerHtml.split("<script>")[1].split("</script>")[0],{
    window:{openai},parent,URL,document:{querySelector:k=>elements[k],body:{scrollHeight:100}},
    addEventListener:(n,f)=>{handlers[n]=f;},ResizeObserver:class{observe(){}},
  });
  const receive=(data,origin="https://chatgpt.example",source=parent)=>handlers.message({source,origin,data:{jsonrpc:"2.0",...data}});
  receive({id:1,result:{hostCapabilities:{openLinks:true}}});
  return {handlers,messages,elements,receive};
}

test("an MCP tool result displays image bytes locally and opens its protected link through the host",()=>{
  const {elements,messages,receive}=viewer();
  receive({method:"ui/notifications/tool-result",params:result});
  assert.equal(elements.img.hidden,false);
  assert.equal(elements.img.src,"data:image/png;base64,cG5n");
  assert.equal(elements.img.alt,result.structuredContent.filename);
  assert.equal(elements.a.href,result.structuredContent.url);
  let prevented=false;elements.a.onclick({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  assert.equal(messages.at(-1).method,"ui/open-link");
  assert.equal(messages.at(-1).params.url,result.structuredContent.url);
  elements.a.onclick({preventDefault(){}});
  assert.deepEqual(messages.filter(m=>m.method==="ui/open-link").map(m=>m.id),[2,3]);
  receive({method:"ui/notifications/tool-result",params:{isError:true}},"https://other.example");
  receive({method:"ui/notifications/tool-result",params:{isError:true}},"https://chatgpt.example",{});
  assert.equal(elements.img.hidden,false);
  receive({id:4,method:"ui/resource-teardown"});
  assert.equal(elements.img.src,undefined);
  assert.equal(elements.a.href,undefined);
});

test("ChatGPT globals also display images; oversized or undecodable images retain a usable link",()=>{
  const {elements,handlers,receive}=viewer({toolResponseMetadata:{mcp_tool_result:result}});
  assert.equal(elements.img.hidden,false);
  handlers["openai:set_globals"]({detail:{globals:{toolResponseMetadata:{call_tool_result:result}}}});
  assert.equal(elements.img.src,"data:image/png;base64,cG5n");
  elements.img.onerror();
  assert.equal(elements.img.hidden,true);
  assert.equal(elements.a.href,result.structuredContent.url);
  assert.match(elements.span.textContent,/表示できません/);
  receive({method:"ui/notifications/tool-result",params:{structuredContent:{...result.structuredContent,delivery:"link"},content:[]}});
  assert.equal(elements.img.hidden,true);
  assert.equal(elements.img.src,undefined);
  assert.equal(elements.a.hidden,false);
  receive({method:"ui/notifications/tool-result",params:{isError:true,content:[{type:"text",text:"Cannot read saved image (HTTP 404)."}]}});
  assert.equal(elements.a.hidden,true);
  assert.match(elements.span.textContent,/404/);
});

test("structured-only ChatGPT globals show the link without replacing a full MCP image",()=>{
  const globals={toolOutput:result.structuredContent};
  const {elements,handlers,receive}=viewer(globals);
  assert.equal(elements.img.hidden,true);
  assert.equal(elements.a.href,result.structuredContent.url);
  receive({method:"ui/notifications/tool-result",params:result});
  assert.equal(elements.img.hidden,false);
  handlers["openai:set_globals"]({detail:{globals}});
  assert.equal(elements.img.hidden,false);
  assert.equal(elements.img.src,"data:image/png;base64,cG5n");
});

test("invalid or disallowed links do not prevent inline images from rendering",()=>{
  const {elements,receive}=viewer();
  for(const url of ["",undefined,"/view?filename=image.png","not a URL","http://split.example.com/view",
    "javascript:alert(1)","https://split.example.com/other"]){
    receive({method:"ui/notifications/tool-result",params:result});
    receive({method:"ui/notifications/tool-result",params:{...result,structuredContent:{...result.structuredContent,url}}});
    assert.equal(elements.img.hidden,false);
    assert.equal(elements.img.src,"data:image/png;base64,cG5n");
    assert.equal(elements.span.textContent,result.structuredContent.filename);
    assert.equal(elements.a.hidden,true);
    assert.equal(elements.a.href,undefined);
    elements.img.onerror();
    assert.equal(elements.span.textContent,"画像を表示できません。");
    receive({method:"ui/notifications/tool-result",params:{structuredContent:{...result.structuredContent,url},content:[]}});
    assert.equal(elements.img.hidden,true);
    assert.equal(elements.span.textContent,result.structuredContent.filename+"：画像を表示できません。");
  }
});

test("missing image information replaces the loading state and clears a previous result",()=>{
  const {elements,receive}=viewer();
  for(const structuredContent of [undefined,{},null]){
    receive({method:"ui/notifications/tool-result",params:{structuredContent}});
    assert.equal(elements.span.textContent,"画像情報を読み込めませんでした。");
    assert.equal(elements.img.hidden,true);
    assert.equal(elements.a.hidden,true);
    receive({method:"ui/notifications/tool-result",params:result});
    assert.equal(elements.img.hidden,false);
  }
});
