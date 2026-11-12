// Explicit opt-in integration check. No service repo dependency in normal tests or CI.
// First build both repos; then node --experimental-strip-types scripts/media-delivery-smoke.mjs --service-root /path/to/personal-media-delivery --browser
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {parseArgs} from 'node:util';
import {readFile,mkdtemp,writeFile} from 'node:fs/promises';
import {join,resolve,extname} from 'node:path';
import {tmpdir} from 'node:os';
import {randomBytes,createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {createServer} from 'node:http';
import {MediaDelivery} from '../src/media-delivery.ts';
import {remoteMcp} from '../src/remote-mcp.ts';
import {texturedGLB,gaussianSPZ} from '../tests/three-fixtures.mjs';

const {values}=parseArgs({options:{'service-root':{type:'string'},browser:{type:'boolean'}}});
if(!values['service-root'])throw Error('Pass --service-root to opt into a separate, built service checkout.');
const root=resolve(values['service-root']),requireService=createRequire(join(root,'package.json'));
const {Miniflare,Log,LogLevel,convertV4MiniflareOptions}=await import(pathToFileURL(requireService.resolve('miniflare')));
const migrationPackage=join(root,'node_modules/@cloudflare/vitest-plugin');
const migrationEntry=JSON.parse(await readFile(join(migrationPackage,'package.json'),'utf8')).exports['.'].import;
const {readD1Migrations}=await import(pathToFileURL(join(migrationPackage,migrationEntry)));
const data=await mkdtemp(join(tmpdir(),'media-adapter-smoke-'));
const token=randomBytes(32).toString('hex'),owner=randomBytes(32).toString('hex');
const publicOrigin='https://media.example.test',browserOrigin='https://split.example.test';
const job='11111111-1111-4111-8111-111111111111';
const encode=spawnSync('ffmpeg',['-loglevel','error','-f','lavfi','-i','testsrc2=size=320x180:rate=10','-t','1','-an','-c:v','libx264','-pix_fmt','yuv420p',join(data,'sample.mp4')]);
if(encode.status!==0)throw Error('ffmpeg is required for the synthetic video fixture. '+encode.stderr);
const wav=Buffer.alloc(44+16000*2);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);
wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(16000,24);wav.writeUInt32LE(32000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(wav.length-44,40);
for(let i=0;i<16000;i++)wav.writeInt16LE(Math.round(1000*Math.sin(i*2*Math.PI*440/16000)),44+i*2);
const files=[
  {filename:'sample.png',kind:'image',mime:'image/png',bytes:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6qXAAAAAASUVORK5CYII=','base64')},
  {filename:'sample.wav',kind:'audio',mime:'audio/wav',bytes:wav},
  {filename:'sample.mp4',kind:'video',mime:'video/mp4',bytes:await readFile(join(data,'sample.mp4'))},
  {filename:'sample.glb',kind:'3d',mime:'model/gltf-binary',bytes:texturedGLB()},
  {filename:'sample.spz',kind:'3d',mime:'application/octet-stream',bytes:gaussianSPZ()},
].map((f,index)=>({...f,index,subfolder:'',etag:'"source-'+index+'"'}));
let originalGets=0,uploads=0,rpcId=0,mf,server,browser;
const send=async(path,init={})=>{
  const url=new URL(path,browserOrigin);
  if(url.pathname==='/history/'+job)return Response.json({[job]:{status:{completed:true,status_str:'success'},outputs:{node:{files:files.map(f=>({...f,bytes:undefined,type:'output'}))}}}});
  if(url.pathname==='/outputs/'+job){const offset=+(url.searchParams.get('offset')||0),limit=+(url.searchParams.get('limit')||50);return Response.json({items:files.slice(offset,offset+limit).map(({index,filename,kind,subfolder})=>({index,filename,kind,subfolder})),total:files.length,next_offset:offset+limit<files.length?offset+limit:null});}
  const f=files.find(f=>f.filename===url.searchParams.get('filename'));assert.ok(f,path);
  const headers={'Content-Type':f.mime,'Content-Length':String(f.bytes.length),ETag:f.etag};
  if(init.method==='HEAD')return new Response(null,{headers});
  originalGets++;
  const match=/^bytes=(\d+)-(\d+)$/.exec(new Headers(init.headers).get('Range')||'');
  if(!match)return new Response(f.bytes,{headers});
  const start=+match[1],end=Math.min(+match[2],f.bytes.length-1);
  return new Response(f.bytes.subarray(start,end+1),{status:206,headers:{...headers,'Content-Length':String(end-start+1),'Content-Range':`bytes ${start}-${end}/${f.bytes.length}`}});
};
const control=async path=>path==='/split/startup'?Response.json({ready:true}):path==='/_remote_mcp/progress'?Response.json({prompt_id:job,status:'completed'}):assert.fail('Unexpected generation/control request '+path);
try{
  mf=new Miniflare(convertV4MiniflareOptions({host:'127.0.0.1',port:0,modules:true,modulesRoot:root,scriptPath:join(root,'.local/worker.mjs'),compatibilityDate:'2026-11-06',log:new Log(LogLevel.ERROR),d1Databases:['DB'],kvNamespaces:['MEDIA'],resourcePersistencePath:join(data,'storage'),
    bindings:{PUBLIC_ORIGIN:publicOrigin,ACCESS_ISSUER:'https://access.example.invalid',ACCESS_AUDIENCE:'local',OWNER_EMAIL:'local',LOCAL_OWNER_TOKEN:owner,SERVICE_TOKENS:JSON.stringify([{source_id:'adapter-smoke',token_hash:createHash('sha256').update(token).digest('hex')}])},
    serviceBindings:{ASSETS:async request=>{const path=new URL(request.url).pathname;if(!/^\/(index\.html|licenses\.txt|assets\/[\w.-]+)$/.test(path))return new Response(null,{status:404});try{return new Response(await readFile(join(root,'.cloudflare/output/v0/workers/default/assets',path)),{headers:{'Content-Type':{'.html':'text/html','.js':'text/javascript','.css':'text/css'}[extname(path)]||'text/plain'}});}catch{return new Response(null,{status:404});}}}}));
  const localOrigin=(await mf.ready).origin,db=await mf.getD1Database('DB');
  for(const migration of await readD1Migrations(join(root,'migrations')))await db.batch(migration.queries.map(sql=>db.prepare(sql)));
  const fetcher=async request=>{
    assert.equal(new URL(request.url).origin,publicOrigin);
    for(const key of ['cookie','cf-access-jwt-assertion','modal-key','modal-secret'])assert.equal(request.headers.get(key),null);
    if(request.method==='PUT')uploads++;
    // Only this test transport maps HTTPS to a loopback endpoint. Production config remains strict HTTPS.
    return fetch(new Request(localOrigin+new URL(request.url).pathname,{method:request.method,headers:request.headers,body:request.body,redirect:'manual',signal:request.signal,duplex:'half'}));
  };
  const rpc=async(method,params)=>{
    const adapter=new MediaDelivery(JSON.stringify({origin:publicOrigin,source_id:'adapter-smoke',token}),fetcher);
    const response=await remoteMcp(new Request(browserOrigin+'/mcp',{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json, text/event-stream'},body:JSON.stringify({jsonrpc:'2.0',id:++rpcId,method,params})}),control,browserOrigin,undefined,send,adapter);
    const message=await response.json();assert.ok(!message.error,JSON.stringify(message));return message.result;
  };
  const call=async(name,args)=>{const result=await rpc('tools/call',{name,arguments:args});assert.ok(!result.isError,JSON.stringify(result));return result;};
  const selections=files.map(f=>({name:'get_'+f.kind,args:f.kind==='audio'||f.kind==='video'?{prompt_id:job}:{filename:f.filename}}));
  const results=[];
  for(const s of selections){const result=await call(s.name,s.args);assert.equal(result.structuredContent.delivery_status,'ready');results.push(result);}
  assert.equal(originalGets,5);assert.equal(uploads,5);
  for(const s of selections)await call(s.name,s.args);
  assert.equal(originalGets,5);assert.equal(uploads,5);
  for(let i=1;i<files.length;i++){
    const f=files[i],ref=results[i].structuredContent.delivery_ref,chunks=[];let offset=0;
    do{const r=await call('read_'+f.kind+'_part',{...selections[i].args,byte_offset:offset,delivery_ref:ref});chunks.push(Buffer.from(r._meta['split/'+f.kind+'Part'],'base64'));offset=r.structuredContent.next_offset;}while(offset!==null);
    assert.deepEqual(Buffer.concat(chunks),f.bytes);
  }
  const completed=await call('get_job_progress',{prompt_id:job});assert.equal(completed.structuredContent.outputs.length,4);assert.equal(completed.structuredContent.outputs_next_offset,4);
  const next=await call('get_job_progress',{prompt_id:job,outputs_offset:4});assert.equal(next.structuredContent.outputs.length,1);
  assert.equal((await fetch(localOrigin+'/v1/media/'+results[0].structuredContent.delivery_ref.media_id+'/content')).status,401);
  assert.equal(originalGets,5);assert.equal(uploads,5);
  const tools=(await rpc('tools/list',{})).tools;
  const templates={};for(const s of selections){if(templates[s.name])continue;const uri=tools.find(t=>t.name===s.name)._meta.ui.resourceUri;templates[s.name]=(await rpc('resources/read',{uri})).contents[0].text;}
  if(values.browser){
    server=createServer(async(req,res)=>{try{const url=new URL(req.url,'http://localhost');let response;
      if(url.pathname==='/rpc'){let body='';for await(const b of req)body+=b;const {name,args}=JSON.parse(body);response=Response.json(await call(name,args));}
      else if(url.pathname==='/widget')response=new Response(templates[selections[+(url.searchParams.get('index')||0)].name],{headers:{'Content-Type':'text/html'}});
      else{const index=+(url.searchParams.get('index')||0);response=new Response(`<!doctype html><title>Media adapter smoke</title><iframe title="MCP viewer" style="width:95vw;height:90vh;border:0" src="/widget?index=${index}"></iframe><script>
      const frame=document.querySelector('iframe');addEventListener('message',async e=>{if(e.source!==frame.contentWindow)return;const m=e.data,reply=result=>frame.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,result},location.origin);
      if(m.method==='ui/initialize'){reply({hostCapabilities:{serverTools:{},openLinks:{}}});frame.contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:${JSON.stringify(results[index]).replaceAll('<','\\u003c')}},location.origin);}
      else if(m.method==='tools/call')reply(await fetch('/rpc',{method:'POST',body:JSON.stringify({name:m.params.name,args:m.params.arguments})}).then(r=>r.json()));});</script>`,{headers:{'Content-Type':'text/html'}});}
      res.writeHead(response.status,Object.fromEntries(response.headers));res.end(Buffer.from(await response.arrayBuffer()));
    }catch{res.writeHead(500);res.end('Smoke request failed');}});
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const {chromium}=requireService('@playwright/test');
    browser=await chromium.launch({channel:process.env.PMD_BROWSER_CHANNEL || 'chrome',headless:true,args:['--enable-unsafe-swiftshader']});const page=await browser.newPage({viewport:{width:1100,height:800}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    for(let i=0;i<files.length;i++){
      await page.goto(`http://127.0.0.1:${server.address().port}/?index=${i}`);const frame=page.frameLocator('iframe');
      if(i===0)await frame.locator('img').evaluate(el=>new Promise((ok,bad)=>{if(el.complete&&el.naturalWidth)return ok();el.onload=ok;el.onerror=bad;}));
      else if(i===1){await frame.locator('#transport:not([hidden])').waitFor();await frame.locator('#play').click();await frame.locator('input:not([disabled])').waitFor();}
      else if(i===2){await frame.locator('video').evaluate(el=>new Promise((ok,bad)=>{if(el.readyState>=2)return ok();el.onloadeddata=ok;el.onerror=bad;}));await frame.locator('video').evaluate(el=>el.play());}
      else {
        await frame.locator('#viewport[data-state="ready"]').waitFor({timeout:60000});
        // Spark schedules its first sorted frame after decoding; capture after that draw.
        if(files[i].filename.endsWith('.spz'))await page.waitForTimeout(1500);
      }
      assert.equal(await frame.locator('a:not([hidden])').last().getAttribute('href'),results[i].structuredContent.url);
      await page.screenshot({path:join(data,files[i].filename+'.png')});
    }
    await page.context().addCookies([{name:'pmd_local',value:owner,url:localOrigin,httpOnly:true,sameSite:'Strict'}]);
    await page.goto(localOrigin+new URL(results[3].structuredContent.url).pathname);
    await page.getByText('Download to open in a 3D viewer',{exact:true}).waitFor();
    await page.screenshot({path:join(data,'private-3d-page.png')});
    assert.deepEqual(errors,[]);
  }
  const report={passed:true,formats:files.map(f=>f.filename),original_body_gets:originalGets,uploads,browser:!!values.browser,artifacts:data};
  await writeFile(join(data,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
}finally{await browser?.close();await new Promise(resolve=>server?server.close(resolve):resolve());await mf?.dispose();}
