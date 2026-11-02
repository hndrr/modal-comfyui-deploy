import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {invoke} from './remote-call.mjs';

test('CPU runs the pinned package against its loopback split gateway',async t=>{
  const dir=await mkdtemp(join(tmpdir(),'split-remote-'));
  t.after(()=>rm(dir,{recursive:true,force:true}));
  const requests=[];
  const server=createServer((req,res)=>{
    requests.push([req.url,req.headers['x-modal-execution-mode']]);
    res.setHeader('content-type','application/json');
    res.end(JSON.stringify({EmptyImage:{name:'EmptyImage',display_name:'EmptyImage',input:{required:{width:['INT']}},output:['IMAGE'],output_name:['IMAGE'],category:'image'}}));
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  t.after(()=>new Promise(r=>server.close(r)));
  const result=await invoke({name:'create_workflow',arguments:{action:'node_info',node_type:'EmptyImage',refresh:true}},
    dir,'http://127.0.0.1:'+server.address().port);
  assert.ok(!result.isError,JSON.stringify(result.content));
  assert.match(result.content[0].text,/width/);
  assert.ok(requests.some(([url,mode])=>url.startsWith('/object_info') && mode==='split'));
  await assert.rejects(invoke({name:'get_image',arguments:{action:'get',save_dir:'/tmp'}},dir));
});

test('saved workflows use the pinned library with encoded names and no path access',async t=>{
  const dir=await mkdtemp(join(tmpdir(),'split-workflow-'));
  t.after(()=>rm(dir,{recursive:true,force:true}));
  const name='日本語/短い 動画.json', graph={'1':{class_type:'EmptyImage',inputs:{width:64}}};
  const seen=[];
  const server=createServer((req,res)=>{
    seen.push(req.url);
    res.setHeader('content-type','application/json');
    if (decodeURIComponent(req.url).includes(name)) res.end(JSON.stringify(graph));
    else res.end(JSON.stringify([name]));
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  t.after(()=>new Promise(r=>server.close(r)));
  const invokeWorkflow=args=>invoke({name:'get_workflow',arguments:args},dir,'http://127.0.0.1:'+server.address().port);
  for(const filename of ['/absolute.json','../secrets.json','a/%2e%2e/file.json','C:/private.json']){
    await assert.rejects(invokeWorkflow({action:'get',filename}));
  }
  await assert.rejects(invokeWorkflow({action:'list',path:'/tmp'}));
  const listing=await invokeWorkflow({action:'list'});
  assert.match(listing.content[0].text,/日本語/);
  for(const format of ['api','ui']){
    const result=await invokeWorkflow({action:'get',filename:name,format});
    assert.deepEqual(JSON.parse(result.content[0].text),graph);
  }
  assert.ok(seen.some(u=>u.includes(encodeURIComponent('workflows/'+name))));
});
