import assert from 'node:assert/strict';
import {test} from 'node:test';
import {randomUUID} from 'node:crypto';
import {MediaDelivery} from '../src/media-delivery.ts';
import {callSplit,remoteMcp} from '../src/remote-mcp.ts';
import {deliveryOutputs} from '../src/delivered-media.ts';

const origin='https://media.example.test', browser='https://split.example.test', token='x'.repeat(64);
const config=JSON.stringify({origin,source_id:'test',token});
const job='abcdef12-1111-4111-8111-111111111111';
const png=Buffer.from('89504e470d0a1a0a0000000000000000','hex');
function mediaBytes(mime,size,fill=1) {
  const bytes=Buffer.alloc(size,fill);
  if(mime==='audio/wav') {bytes.write('RIFF',0);bytes.write('WAVE',8);}
  if(mime==='video/mp4') bytes.write('ftyp',4);
  return bytes;
}
function fixture(count=1){
  const records=new Map(),calls=[],original=[];
  const files=Array.from({length:count},(_,index)=>({filename:`image${index}.png`,subfolder:'',index,kind:'image',mime:'image/png',bytes:png,etag:'"source-v1"'}));
  const value=r=>r.state==='ready'?{status:'ready',media_id:r.id,url:origin+'/media/'+r.id,mime_type:r.mime,
    size_bytes:r.bytes.length,etag:r.etag,expires_at:'2026-12-01T00:00:00.000Z',cleansed:r.cleansed??false}:
    {status:'upload_required',media_id:r.id,upload_id:r.upload};
  const fetcher=async request=>{
    const url=new URL(request.url);calls.push({path:url.pathname,method:request.method,headers:Object.fromEntries(request.headers)});
    assert.equal(url.origin,origin);assert.equal(request.redirect,'manual');
    assert.equal(request.headers.get('Authorization'),'Bearer '+token);
    for(const key of ['cookie','cf-access-jwt-assertion','modal-key','modal-secret'])assert.equal(request.headers.get(key),null);
    if(url.pathname==='/v1/media/lookup'){
      const {source_id,references}=await request.json();assert.equal(source_id,'test');assert.ok(references.length<=24);
      return Response.json({results:references.map(ref=>{const r=records.get(JSON.stringify(ref));return r?.state==='ready'?value(r):{status:'missing',reason:r?'expired':'not_found'};})});
    }
    if(url.pathname==='/v1/media'){
      const p=await request.json();assert.equal(p.source_id,'test');
      assert.deepEqual(Object.keys(p).sort(),['source_id','output_path','source_etag','filename','mime_type','size_bytes','kind'].sort());
      const key=JSON.stringify({output_path:p.output_path,source_etag:p.source_etag});
      let r=records.get(key);if(!r){r={id:randomUUID(),upload:randomUUID(),mime:p.mime_type,size:p.size_bytes,state:'reserved',etag:'"copy-v1"'};records.set(key,r);}
      return Response.json(value(r));
    }
    const id=url.pathname.split('/')[3],r=[...records.values()].find(x=>x.id===id);assert.ok(r);
    if(request.method==='PUT'){
      assert.equal(request.headers.get('X-Upload-Id'),r.upload);assert.equal(request.headers.get('Content-Type'),r.mime);
      r.bytes=Buffer.from(await request.arrayBuffer());assert.equal(r.bytes.length,r.size);r.state='ready';return Response.json(value(r));
    }
    const [,a,b]=/^bytes=(\d+)-(\d+)$/.exec(request.headers.get('Range'));
    if(request.headers.get('If-Range')!==r.etag)return new Response(r.bytes,{headers:{ETag:r.etag}});
    const end=Math.min(+b,r.bytes.length-1);
    return new Response(r.bytes.subarray(+a,end+1),{status:206,headers:{'Content-Range':`bytes ${a}-${end}/${r.bytes.length}`,ETag:r.etag,'Content-Type':r.mime}});
  };
  const send=async(path,init)=>{
    original.push({path,method:init.method});
    const url=new URL(path,browser);
    if(path==='/history/'+job)return Response.json({[job]:{status:{completed:true,status_str:'success'},outputs:{node:{images:files.map(({filename,subfolder})=>({filename,subfolder,type:'output'}))}}}});
    if(url.pathname==='/outputs/'+job){const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||50);return Response.json({items:files.slice(offset,offset+limit).map(({index,filename,subfolder,kind})=>({index,filename,subfolder,kind})),total:files.length,next_offset:offset+limit<files.length?offset+limit:null});}
    assert.equal(url.pathname,'/view');const f=files.find(x=>x.filename===url.searchParams.get('filename'));assert.ok(f);
    const headers={'Content-Type':f.mime,'Content-Length':String(f.bytes.length),ETag:f.etag};
    if(init.method==='HEAD')return new Response(null,{headers});
    const range=new Headers(init.headers).get('Range');
    if(range){const [,a,b]=/^bytes=(\d+)-(\d+)$/.exec(range),end=Math.min(+b,f.bytes.length-1);return new Response(f.bytes.subarray(+a,end+1),{status:206,headers:{...headers,'Content-Length':String(end-Number(a)+1),'Content-Range':`bytes ${a}-${end}/${f.bytes.length}`}});}
    return new Response(f.bytes,{headers});
  };
  return {records,calls,original,files,fetcher,send,adapter:()=>new MediaDelivery(config,fetcher)};
}
const noGeneration=async()=>{throw Error('Unexpected Modal control-plane request');};

test('original-route CLI chunks continue after fallback, and CPU readiness is checked only once per call',async()=>{
  for(const separate of [true,false]) {
    const f=fixture();Object.assign(f.files[0],{kind:'audio',filename:'clip.wav',mime:'audio/wav',bytes:mediaBytes('audio/wav',6*1024*1024)});
    let startups=0,external=0;
    const control=async(p,i)=>p==='/split/startup'?(startups++,Response.json({ready:true})):f.send(p,i);
    const adapter=()=>new MediaDelivery(config,async()=>{external++;return new Response(null,{status:503});});
    const first=await callSplit(control,'get_audio',{prompt_id:job,byte_offset:0},browser,undefined,separate?f.send:undefined,adapter());
    assert.equal(first.isError,undefined,first.content[0].text);assert.equal(first.structuredContent.next_offset,4*1024*1024);
    assert.equal(first.structuredContent.delivery_ref,undefined);assert.equal(startups,separate?0:1);
    assert.equal(first.structuredContent.delivery_route,'original');
    // A copy becoming ready between calls must not change an original-route read.
    await f.adapter().prepare(f.send,f.files[0]);
    Object.assign([...f.records.values()][0],{bytes:mediaBytes('audio/wav',5*1024*1024,2),cleansed:true});
    const next=await callSplit(control,'get_audio',{prompt_id:job,byte_offset:first.structuredContent.next_offset,delivery_route:first.structuredContent.delivery_route},browser,undefined,separate?f.send:undefined,adapter());
    assert.equal(next.isError,undefined);assert.equal(next.structuredContent.next_offset,null);
    assert.equal(startups,separate?0:2);assert.equal(external,1);
    assert.deepEqual(Buffer.from(next.content[1].resource.blob,'base64'),f.files[0].bytes.subarray(4*1024*1024));
  }
});

test('copy references cannot be reused for another audio output or job',async()=>{
  const f=fixture();Object.assign(f.files[0],{kind:'audio',filename:'clip.wav',mime:'audio/wav',bytes:mediaBytes('audio/wav',32)});
  const first=await callSplit(noGeneration,'get_audio',{prompt_id:job},browser,undefined,f.send,f.adapter());
  assert.equal(first.isError,undefined);const ref=first.structuredContent.delivery_ref;
  f.calls.length=0;f.original.length=0;
  for(const args of [{prompt_id:job,index:1},{prompt_id:randomUUID()},{prompt_id:job,delivery_ref:{...ref,media_id:'bad'}}]) {
    const result=await callSplit(noGeneration,'read_audio_part',{delivery_ref:ref,byte_offset:0,...args},browser,undefined,f.send,f.adapter());
    assert.equal(result.isError,true);assert.equal(result._meta,undefined);
  }
  assert.equal(f.calls.length,0);assert.equal(f.original.length,0);
});

test('source paths outside the service contract fall back individually without a retry loop',async()=>{
  const f=fixture(3);f.files[1].subfolder=Array(4).fill('a'.repeat(200)).join('/');
  for(const prepare of [false,true]) {
    const out=await deliveryOutputs(f.adapter(),f.send,{items:f.files,total:3,next_offset:null},prepare,browser);
    assert.equal(out.items.length,3);assert.equal(out.items[1].delivery_status,'unavailable');
    assert.equal(out.items[1].delivery_reason,'invalid_media_reference');assert.equal(out.items[1].retry_after_seconds,undefined);
    assert.equal(out.items[0].delivery_status,prepare?'ready':'unavailable');
    assert.equal(new URL(out.items[1].url).origin,browser);
  }
  Object.assign(f.files[0],{kind:'audio',filename:'clip.wav',mime:'audio/wav',subfolder:'.hidden',bytes:mediaBytes('audio/wav',32)});
  const result=await callSplit(noGeneration,'get_audio',{prompt_id:job},browser,undefined,f.send,f.adapter());
  assert.equal(result.isError,undefined);assert.equal(result.structuredContent.delivery_reason,'invalid_media_reference');assert.equal(result._meta,undefined);
});

test('source WAV aliases and normalized copy Content-Type preserve format validation',async()=>{
  const f=fixture();Object.assign(f.files[0],{kind:'audio',filename:'clip.wav',mime:'audio/x-wav',bytes:mediaBytes('audio/wav',32)});
  const adapter=new MediaDelivery(config,async req=>{
    const response=await f.fetcher(req);
    if(req.method==='GET')response.headers.set('Content-Type','Audio/WAV ; charset=binary');
    return response;
  });
  const result=await callSplit(noGeneration,'get_audio',{prompt_id:job},browser,undefined,f.send,adapter);
  assert.equal(result.isError,undefined);assert.equal(result.structuredContent.mimeType,'audio/wav');
  assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
  for(const [kind,filename,mime] of [['image','image0.png','image/png'],['audio','clip.wav','audio/wav'],['video','clip.mp4','video/mp4']]) {
    const broken=fixture();Object.assign(broken.files[0],{kind,filename,mime,bytes:kind==='image'?png:mediaBytes(mime,32)});
    await broken.adapter().prepare(broken.send,broken.files[0]);
    [...broken.records.values()][0].bytes.fill(0);
    for(let attempt=0;attempt<2;attempt++) {
      const out=await callSplit(noGeneration,'get_'+kind,kind==='image'?{filename}:{prompt_id:job},browser,undefined,broken.send,broken.adapter());
      assert.equal(out.isError,undefined);assert.equal(out.structuredContent.delivery_reason,'invalid_media_content');
      assert.equal(out.structuredContent.delivery_route,'original');assert.equal(new URL(out.structuredContent.url).origin,browser);
    }
  }
});

test('a missing copy reference never splices original bytes into an audio or video continuation',async()=>{
  for(const [kind,mime,filename] of [['audio','audio/wav','clip.wav'],['video','video/mp4','clip.mp4']]) {
    const f=fixture();Object.assign(f.files[0],{kind,mime,filename,bytes:mediaBytes(mime,6*1024*1024)});
    await f.adapter().prepare(f.send,f.files[0]);
    const copy=[...f.records.values()][0];Object.assign(copy,{bytes:mediaBytes(mime,5*1024*1024,2),cleansed:true});
    const first=await callSplit(noGeneration,'get_'+kind,{prompt_id:job,byte_offset:0},browser,undefined,f.send,f.adapter());
    assert.equal(first.isError,undefined);assert.equal(first.structuredContent.size,copy.bytes.length);
    f.original.length=0;f.calls.length=0;
    for(const name of ['get_'+kind,`read_${kind}_part`]) {
      const missing=await callSplit(noGeneration,name,{prompt_id:job,byte_offset:first.structuredContent.next_offset},browser,undefined,f.send,f.adapter());
      assert.equal(missing.isError,true);assert.match(missing.content[0].text,/delivery_reference_required/);
    }
    assert.equal(f.original.length,0);assert.equal(f.calls.length,0);
    const next=await callSplit(noGeneration,'get_'+kind,{prompt_id:job,byte_offset:first.structuredContent.next_offset,delivery_ref:first.structuredContent.delivery_ref},browser,undefined,f.send,f.adapter());
    assert.equal(next.isError,undefined);assert.equal(next.structuredContent.next_offset,null);
    assert.deepEqual(Buffer.concat([first,next].map(r=>Buffer.from(r.content[1].resource.blob,'base64'))),copy.bytes);
    assert.equal(f.original.length,0);
  }
});

test('missing ready content falls back only before selecting a copy and explicit copy reads never fall back',async()=>{
  const f=fixture();Object.assign(f.files[0],{kind:'audio',mime:'audio/wav',filename:'clip.wav',bytes:mediaBytes('audio/wav',64)});
  const ready=await f.adapter().prepare(f.send,f.files[0]);
  const adapter=()=>new MediaDelivery(config,req=>req.method==='GET'?Promise.resolve(new Response(null,{status:404})):f.fetcher(req));
  for(const chunk of [false,true]) {
    const result=await callSplit(noGeneration,'get_audio',{prompt_id:job,...(chunk?{byte_offset:0}:{})},browser,undefined,f.send,adapter());
    assert.equal(result.isError,undefined);assert.equal(result.structuredContent.delivery_route,'original');
    assert.equal(result.structuredContent.delivery_reason,'copy_changed_or_unavailable');
  }
  const ref={...ready,filename:'clip.wav',subfolder:'',prompt_id:job,index:0};
  for(const offset of [0,16]) {
    const result=await callSplit(noGeneration,'read_audio_part',{prompt_id:job,byte_offset:offset,delivery_ref:ref},browser,undefined,noGeneration,adapter());
    assert.equal(result.isError,true);assert.match(result.content[0].text,/copy_changed_or_unavailable/);
  }
  const conflict=await callSplit(noGeneration,'get_audio',{prompt_id:job,byte_offset:16,delivery_route:'original',delivery_ref:ref},browser,undefined,noGeneration,adapter());
  assert.equal(conflict.isError,true);assert.match(conflict.content[0].text,/invalid_delivery_reference/);
});

test('completed output lookup and HEAD failures preserve retry information without changing job status',async()=>{
  const control=async path=>path==='/split/startup'?Response.json({ready:true}):Response.json({prompt_id:job,status:'completed'});
  for(const failed of ['lookup','head']) {
    const f=fixture(2);f.files[1].filename='unsupported.mov';
    const adapter=new MediaDelivery(config,async()=>Response.json({error:{code:'service_unavailable',retry_after_seconds:17}},{status:503}));
    const source=(p,i)=>{if(failed==='head' && i.method==='HEAD')throw new DOMException('timeout','TimeoutError');return f.send(p,i);};
    const result=await callSplit(control,'get_job_progress',{prompt_id:job},browser,undefined,source,adapter);
    assert.equal(result.structuredContent.status,'completed');
    const [retry,unsupported]=result.structuredContent.outputs;
    assert.equal(retry.delivery_status,'unavailable');assert.equal(retry.retry_after_seconds,failed==='lookup'?17:60);
    assert.equal(unsupported.delivery_reason,'unsupported_media');assert.equal(unsupported.retry_after_seconds,undefined);
    assert.equal(result.structuredContent.outputs_next_offset,null);
  }
});

test('uppercase completed job IDs resolve output pages using the canonical lowercase ID',async()=>{
  const f=fixture();
  const control=async path=>path==='/split/startup'?Response.json({ready:true}):Response.json({prompt_id:job,status:'completed'});
  const result=await callSplit(control,'get_job_progress',{prompt_id:job.toUpperCase()},browser,undefined,f.send,f.adapter());
  assert.equal(result.structuredContent.status,'completed');assert.equal(result.structuredContent.outputs[0].delivery_status,'ready');
});

test('fixed configuration rejects unsafe origins and invalid credentials before any fetch',()=>{
  for(const origin of ['http://media.example.test','https://u:p@media.example.test','https://media.example.test/path','https://media.example.test?x=1','https://media.example.test:8443'])
    assert.throws(()=>new MediaDelivery(JSON.stringify({origin,source_id:'s',token})));
  assert.throws(()=>new MediaDelivery(JSON.stringify({origin,source_id:'s',token:'bad'})));
});

test('first image streams one copy, repeated retrieval and registration reuse it, disabling restores the original',async()=>{
  const f=fixture(),args={filename:f.files[0].filename};
  const first=await callSplit(noGeneration,'get_image',args,browser,undefined,f.send,f.adapter());
  assert.equal(first.isError,undefined);assert.equal(first.structuredContent.delivery_status,'ready');
  assert.deepEqual(Buffer.from(first.content[0].data,'base64'),png);
  assert.match(first.structuredContent.url,/^https:\/\/media.example.test\/media\//);
  assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
  f.original.length=0;
  const second=await callSplit(noGeneration,'get_image',args,browser,undefined,f.send,f.adapter());
  assert.deepEqual(second.structuredContent.delivery_ref,first.structuredContent.delivery_ref);
  assert.equal(f.original.filter(c=>c.method==='GET').length,0);
  assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
  const before=f.calls.length;
  const legacy=await callSplit(noGeneration,'get_image',args,browser,undefined,f.send);
  assert.equal(new URL(legacy.structuredContent.url).origin,browser);assert.equal(f.calls.length,before);
});

test('expired copies recopy the original without resubmitting generation',async()=>{
  const f=fixture();await f.adapter().prepare(f.send,f.files[0]);
  [...f.records.values()][0].state='expired';
  const state=await f.adapter().prepare(f.send,f.files[0]);assert.equal(state.status,'ready');
  assert.equal(f.records.size,1);assert.equal(f.calls.filter(c=>c.method==='PUT').length,2);
});

test('audio, video, GLB and SPZ parts read only the selected external copy and pin its version',async()=>{
  for(const [kind,filename,mime] of [['audio','clip.wav','audio/wav'],['video','clip.mp4','video/mp4'],['3d','model.glb','model/gltf-binary'],['3d','model.spz','application/octet-stream']]){
    const f=fixture();Object.assign(f.files[0],{kind,filename,mime,bytes:mediaBytes(mime,600000)});
    const args=kind==='3d'?{filename}:{prompt_id:job};
    const first=await callSplit(noGeneration,'get_'+kind,args,browser,undefined,f.send,f.adapter());
    assert.equal(first.isError,undefined,JSON.stringify(first));assert.ok(first.structuredContent.delivery_ref);
    f.original.length=0;
    const chunk=await callSplit(noGeneration,`read_${kind}_part`,{...args,byte_offset:0,delivery_ref:first.structuredContent.delivery_ref},browser,undefined,f.send,f.adapter());
    assert.equal(chunk.isError,undefined);assert.deepEqual(chunk.content,[]);assert.equal(f.original.length,0);
    assert.ok(chunk._meta[`split/${kind}Part`]);assert.equal(chunk.structuredContent.etag,'"copy-v1"');
    [...f.records.values()][0].etag='"copy-v2"';
    const changed=await callSplit(noGeneration,`read_${kind}_part`,{...args,byte_offset:chunk.structuredContent.next_offset,delivery_ref:first.structuredContent.delivery_ref},browser,undefined,f.send,f.adapter());
    assert.equal(changed.isError,true);assert.match(changed.content[0].text,/changed/);assert.equal(f.original.length,0);
  }
});

test('current cleansed metadata is authoritative while lookup still uses the original version',async()=>{
  const f=fixture();await f.adapter().prepare(f.send,f.files[0]);const copy=[...f.records.values()][0];
  Object.assign(copy,{bytes:png.subarray(0,12),mime:'image/jpeg',etag:'"clean-v2"',cleansed:true});
  const ready=await f.adapter().prepare(f.send,f.files[0]);assert.equal(ready.size_bytes,12);assert.equal(ready.etag,'"clean-v2"');assert.equal(ready.mime_type,'image/jpeg');
  assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
});

test('job-based retrieval detects an overwritten output even while the previous copy is ready',async()=>{
  for(const [kind,filename,mime] of [['audio','clip.wav','audio/wav'],['video','clip.mp4','video/mp4']]){
    const f=fixture();Object.assign(f.files[0],{kind,filename,mime,bytes:mediaBytes(mime,16)});
    const args={prompt_id:job};
    const first=await callSplit(noGeneration,'get_'+kind,args,browser,undefined,f.send,f.adapter());
    assert.equal(first.isError,undefined);
    const oldCopy=[...f.records.values()][0];
    Object.assign(f.files[0],{etag:'"source-v2"',bytes:mediaBytes(mime,24,2)});
    f.original.length=0;
    const current=await callSplit(noGeneration,'get_'+kind,args,browser,undefined,f.send,f.adapter());
    assert.equal(current.isError,undefined);assert.equal(current.structuredContent.delivery_status,'ready');
    assert.notEqual(current.structuredContent.delivery_ref.media_id,first.structuredContent.delivery_ref.media_id);
    assert.equal(current.structuredContent.size,24);assert.equal(f.records.size,2);
    assert.equal(oldCopy.state,'ready');assert.deepEqual(oldCopy.bytes,mediaBytes(mime,16));
    assert.equal(f.original.filter(c=>c.method==='GET' && c.path.startsWith('/view?')).length,1);
    assert.equal(f.calls.filter(c=>c.method==='PUT').length,2);
    f.original.length=0;
    const reused=await callSplit(noGeneration,'get_'+kind,args,browser,undefined,f.send,f.adapter());
    assert.deepEqual(reused.structuredContent.delivery_ref,current.structuredContent.delivery_ref);
    assert.equal(f.original.filter(c=>c.method==='GET' && c.path.startsWith('/view?')).length,0);
    assert.equal(f.calls.filter(c=>c.method==='PUT').length,2);
  }
});

test('pending transfer does not start another PUT or return a premature URL',async()=>{
  const f=fixture();const adapter=new MediaDelivery(config,async req=>{
    if(req.url.endsWith('/lookup'))return Response.json({results:[{status:'pending',media_id:job,retry_after_seconds:60}]});
    assert.fail('Pending copies must not be registered or uploaded again');
  });
  const result=await callSplit(noGeneration,'get_image',{filename:f.files[0].filename},browser,undefined,f.send,adapter);
  assert.equal(result.isError,true);assert.equal(result._meta.retry_after_seconds,60);
  assert.equal(f.original.filter(x=>x.method==='GET').length,0);
});

test('size limits, service errors and redirects retain the authenticated original without leaking response secrets',async()=>{
  for(const response of [()=>new Response('TOP_SECRET',{status:503}),()=>new Response(null,{status:302,headers:{Location:'https://attacker.example'}}),()=>Response.json({results:[{status:'ready',media_id:job,url:'https://attacker.example',mime_type:'image/png',size_bytes:16,etag:'"copy"',expires_at:null,cleansed:false}]})]){
    const f=fixture(),adapter=new MediaDelivery(config,async()=>response());
    const result=await callSplit(noGeneration,'get_image',{filename:f.files[0].filename},browser,undefined,f.send,adapter);
    assert.equal(result.isError,undefined);assert.equal(result.structuredContent.delivery_status,'unavailable');
    assert.equal(new URL(result.structuredContent.url).origin,browser);assert.ok(!JSON.stringify(result).includes('TOP_SECRET'));
  }
  const f=fixture();const send=(path,init)=>init.method==='HEAD'?Promise.resolve(new Response(null,{headers:{'Content-Length':String(26*1024*1024)}})):f.send(path,init);
  const result=await callSplit(noGeneration,'get_image',{filename:f.files[0].filename},browser,undefined,send,f.adapter());
  assert.equal(result.structuredContent.delivery_reason,'file_too_large');assert.equal(f.calls.length,0);
});

test('source replacement between HEAD and GET is rejected before uploading bytes',async()=>{
  const f=fixture();const send=(p,i)=>{if(i.method==='GET')f.files[0].etag='"replaced"';return f.send(p,i);};
  await assert.rejects(f.adapter().prepare(send,f.files[0]),/source_changed/);
  assert.equal(f.calls.filter(c=>c.method==='PUT').length,0);
});

test('lost upload acknowledgement is resolved by lookup; no second original read or PUT',async()=>{
  const f=fixture();const adapter=new MediaDelivery(config,async req=>{const r=await f.fetcher(req);if(req.method==='PUT')throw Error('lost acknowledgement');return r;});
  await assert.rejects(adapter.prepare(f.send,f.files[0]),/service_unavailable/);f.original.length=0;
  assert.equal((await f.adapter().prepare(f.send,f.files[0])).status,'ready');
  assert.equal(f.original.filter(c=>c.method==='GET').length,0);assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
});

test('output listing caps pages at 32 and splits lookup into 24/8 without writing or reading original bodies',async()=>{
  const f=fixture(50);
  const result=await callSplit(noGeneration,'list_outputs',{prompt_id:job,limit:100},browser,undefined,f.send,f.adapter());
  const out=JSON.parse(result.content[0].text);
  assert.equal(out.items.length,32);assert.equal(out.next_offset,32);assert.equal(f.calls.length,2);
  assert.equal(out.items[0].delivery_reason,'not_found');
  assert.ok(f.calls.every(c=>c.path==='/v1/media/lookup'));
  assert.equal(f.original.filter(c=>c.method==='GET' && c.path.startsWith('/view')).length,0);
  assert.equal(f.original.filter(c=>c.method==='HEAD').length,32);
});

test('completed progress prepares four outputs, retains failures and pages without resubmission',async()=>{
  const f=fixture(5);f.files[1].filename='unsupported.mov';
  const control=async path=>path==='/split/startup'?Response.json({ready:true}):path==='/_remote_mcp/progress'?Response.json({prompt_id:job,status:'completed'}):assert.fail(path);
  const result=await callSplit(control,'get_job_progress',{prompt_id:job},browser,undefined,f.send,f.adapter());
  const data=result.structuredContent;assert.equal(data.status,'completed');assert.equal(data.outputs.length,4);assert.equal(data.outputs_next_offset,4);
  assert.equal(data.outputs[1].delivery_status,'unavailable');assert.equal(data.outputs[0].delivery_status,'ready');
  const next=await callSplit(control,'get_job_progress',{prompt_id:job,outputs_offset:4},browser,undefined,f.send,f.adapter());
  assert.equal(next.structuredContent.outputs.length,1);assert.equal(next.structuredContent.outputs_next_offset,null);
  assert.equal(f.calls.filter(c=>c.method==='PUT').length,4);
  assert.equal(f.calls.filter(c=>c.path==='/v1/media/lookup').length,2);
});

test('configured tool annotations, output schemas and completion paging expose the actual contract',async()=>{
  const f=fixture();let id=0;
  const rpc=async(method,params,delivery)=>{
    const r=await remoteMcp(new Request(browser+'/mcp',{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json, text/event-stream'},body:JSON.stringify({jsonrpc:'2.0',id:++id,method,params})}),noGeneration,browser,undefined,f.send,delivery);
    return (await r.json()).result;
  };
  for(const enabled of [false,true]){
    const {tools}=await rpc('tools/list',{},enabled?f.adapter():undefined);
    for(const name of ['get_image','get_audio','get_video','get_3d','get_job_progress'])assert.equal(tools.find(t=>t.name===name).annotations.readOnlyHint,!enabled);
    assert.equal('outputs_offset' in tools.find(t=>t.name==='get_job_progress').inputSchema.properties,enabled);
    for(const name of ['get_audio','get_video','read_audio_part','read_video_part','read_3d_part'])
      assert.equal('delivery_route' in tools.find(t=>t.name===name).inputSchema.properties,enabled);
    assert.equal(tools.find(t=>t.name==='visualize_workflow').annotations.readOnlyHint,true);
  }
  const result=await rpc('tools/call',{name:'get_image',arguments:{filename:f.files[0].filename}},f.adapter());
  assert.equal(result.isError,undefined,JSON.stringify(result));assert.equal(result.structuredContent.delivery_status,'ready');
  Object.assign(f.files[0],{filename:'clip.wav',mime:'audio/wav',kind:'audio',bytes:mediaBytes('audio/wav',32)});
  const before=f.calls.length;
  const original=await rpc('tools/call',{name:'get_audio',arguments:{prompt_id:job,byte_offset:16,delivery_route:'original'}},f.adapter());
  assert.equal(original.isError,undefined);assert.equal(original.structuredContent.delivery_route,'original');
  assert.equal(original.structuredContent.byte_offset,16);assert.equal(f.calls.length,before);
});


test('without a media CPU binding, completion and lookup use existing control operations',async()=>{
  const f=fixture(),operations=[];
  const control=async(path,init)=>{
    if(path==='/split/startup')return Response.json({ready:true});
    if(path==='/_remote_mcp/progress')return Response.json({prompt_id:job,status:'completed'});
    if(path==='/_remote_mcp/operations'){
      const operation=JSON.parse(init.body);operations.push(operation);assert.equal(operation.name,'list_outputs');
      return Response.json({items:f.files,total:1,next_offset:null});
    }
    return f.send(path,init);
  };
  const result=await callSplit(control,'get_job_progress',{prompt_id:job},browser,undefined,undefined,f.adapter());
  assert.equal(result.structuredContent.outputs[0].delivery_status,'ready');
  assert.deepEqual(operations[0].arguments,{prompt_id:job,offset:0,limit:4});
  const uploads=f.calls.filter(c=>c.method==='PUT').length;f.original.length=0;
  const list=await callSplit(control,'list_outputs',{prompt_id:job},browser,undefined,undefined,f.adapter());
  assert.equal(JSON.parse(list.content[0].text).items[0].delivery_status,'ready');
  assert.equal(f.calls.filter(c=>c.method==='PUT').length,uploads);
  assert.equal(f.original.filter(c=>c.method==='GET').length,0);
});

test('GLB/SPZ reuse the current cleansed copy without transferring the original again',async()=>{
  for(const [filename,mime] of [['model.glb','model/gltf-binary'],['cloud.spz','application/octet-stream']]){
    const f=fixture();Object.assign(f.files[0],{filename,kind:'3d',mime,bytes:Buffer.alloc(64,1)});
    const original=await callSplit(noGeneration,'get_3d',{filename},browser,undefined,f.send,f.adapter());
    const copy=[...f.records.values()][0];Object.assign(copy,{bytes:Buffer.alloc(32,2),etag:'"clean-3d-v2"',cleansed:true});
    f.original.length=0;
    const current=await callSplit(noGeneration,'get_3d',{filename},browser,undefined,f.send,f.adapter());
    assert.equal(current.structuredContent.etag,'"clean-3d-v2"');assert.equal(current.structuredContent.size,32);
    assert.equal(current.structuredContent.delivery_ref.media_id,original.structuredContent.delivery_ref.media_id);
    const chunk=await callSplit(noGeneration,'read_3d_part',{filename,byte_offset:0,delivery_ref:current.structuredContent.delivery_ref},browser,undefined,f.send,f.adapter());
    assert.deepEqual(Buffer.from(chunk._meta['split/3dPart'],'base64'),copy.bytes);
    assert.equal(f.original.filter(c=>c.method==='GET').length,0);assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
    const old=await callSplit(noGeneration,'read_3d_part',{filename,byte_offset:0,delivery_ref:original.structuredContent.delivery_ref},browser,undefined,f.send,f.adapter());
    assert.equal(old.isError,true);
  }
});
