import {test} from 'node:test';
import assert from 'node:assert/strict';
import {exportJWK,generateKeyPair,SignJWT} from 'jose';
import worker,{buildUpstreamHeaders} from '../src/index.ts';
import {callSplit} from '../src/remote-mcp.ts';

test('OAuth audience and owner guard discovery; discovery and ping never contact Modal',async t=>{
  const team='https://remote-test.cloudflareaccess.com', host='mcp.example.com', split='split.example.com',origin='https://split.modal.run';
  const cfg={TEAM_DOMAIN:team,POLICY_AUD:'ui-aud',MODAL_KEY:'key',MODAL_SECRET:'secret',
    MODAL_ORIGINS:JSON.stringify({[split]:origin}),SPLIT_REMOTE_MCP:JSON.stringify({hostname:host,origin,split_hostname:split,audience:'mcp-aud',owner_email:'owner@example.com'})};
  const {privateKey,publicKey}=await generateKeyPair('RS256');
  const sign=(aud,email,issuer=team)=>new SignJWT({type:'app',sub:'owner',email}).setProtectedHeader({alg:'RS256',kid:'k'})
    .setIssuer(issuer).setAudience(aud).setExpirationTime('5m').sign(privateKey);
  const jwk={...await exportJWK(publicKey),kid:'k'};
  const hits=[];
  t.mock.method(globalThis,'fetch',async r=>{
    if(r===team+'/cdn-cgi/access/certs')return Response.json({keys:[jwk]});
    hits.push(r);
    if(new URL(r.url).pathname==='/split/startup') return Response.json({ready:true,failed:false});
    return Response.json({content:[{type:'text',text:'node definition'}]});
  });
  const request=(jwt,method='tools/list',params)=>worker.fetch(new Request('https://'+host+'/mcp',{method:'POST',
    headers:{'Content-Type':'application/json',Accept:'application/json, text/event-stream',Authorization:'Bearer do-not-forward',
      Cookie:'private=secret','CF-Access-Jwt-Assertion':jwt},
    body:JSON.stringify({jsonrpc:'2.0',id:1,method,params})}),cfg);
  for (const jwt of ['',await sign('ui-aud','owner@example.com'),await sign('mcp-aud','another@example.com'),await sign('mcp-aud','owner@example.com','https://wrong.example.com')])
    assert.equal((await request(jwt)).status,403);
  const jwt=await sign('mcp-aud','owner@example.com');
  const init=await (await request(jwt,'initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'test',version:'1'}})).json();
  assert.equal(init.result.serverInfo.name,'comfyui-split');
  const list=await (await request(jwt)).json();
  for (const name of ["get_image", "get_video", "get_audio"])
    assert.ok(list.result.tools.some(t => t.name === name));
  assert.ok((await (await request(jwt,'ping')).json()).result);
  assert.equal(hits.length,0);
  const r=await (await request(jwt,'tools/call',{name:'create_workflow',arguments:{action:'node_info',node_type:'EmptyImage'}})).json();
  assert.equal(r.result.content[0].text,'node definition');
  assert.deepEqual(hits.map(r=>new URL(r.url).pathname),['/split/startup','/_remote_mcp/call']);
  for(const r of hits) {
    assert.equal(r.redirect,'manual');
    assert.equal(new URL(r.url).origin,origin);
    for(const h of ['Cookie','Authorization','CF-Access-Jwt-Assertion']) assert.equal(r.headers.get(h),null);
    assert.equal(r.headers.get('X-Modal-Execution-Mode'),'split');
  }
  assert.equal(hits[1].headers.get('X-Split-Remote-Tool'),'1');
  assert.equal(buildUpstreamHeaders(new Request(origin,{headers:{'X-Split-Remote-Tool':'1'}}),cfg,origin).get('X-Split-Remote-Tool'),null);

  // After a host rename the UI and MCP share a hostname, never their audiences.
  const shared={...cfg,SPLIT_UI_ORIGIN:JSON.stringify({[host]:origin}),
    SPLIT_REMOTE_MCP:JSON.stringify({...JSON.parse(cfg.SPLIT_REMOTE_MCP),split_hostname:host})};
  const at=(path,assertion,config=shared)=>worker.fetch(new Request('https://'+host+path,{
    headers:{'CF-Access-Jwt-Assertion':assertion}}),config);
  const browser=await sign('ui-aud','owner@example.com');
  hits.length=0;
  assert.equal((await at('/',jwt)).status,403);
  assert.equal((await at('/mcp',browser)).status,403);
  assert.equal((await at('/mcp/',browser)).status,404);
  assert.equal((await at('/mcp',browser,{...shared,SPLIT_REMOTE_MCP:undefined})).status,404);
  assert.equal(hits.length,0);
  assert.equal((await at('/',browser)).status,200);
  assert.deepEqual(hits.map(r=>r.url),[origin+'/']);
  const discovery=await worker.fetch(new Request('https://'+host+'/mcp',{method:'POST',
    headers:{'Content-Type':'application/json',Accept:'application/json, text/event-stream','CF-Access-Jwt-Assertion':jwt},
    body:JSON.stringify({jsonrpc:'2.0',id:2,method:'tools/list'})}),shared);
  assert.equal((await discovery.json()).result.tools.length,7);
  assert.equal(hits.length,1);
});

test('a lost receipt uses exactly the same queue request on retry',async()=>{
  const receipts=new Map();let commits=0,calls=0;
  const send=async(path,init)=>{
    if(path==='/split/startup')return Response.json({ready:true});
    assert.equal(path,'/prompt'); const key=init.headers['Idempotency-Key'];
    assert.equal(key,'remote-mcp:job-1');calls++;
    if(!receipts.has(key)){commits++;receipts.set(key,init.body);throw new Error('connection lost after commit');}
    return receipts.get(key)===init.body?Response.json({prompt_id:'original',number:1}):new Response(null,{status:409});
  };
  const args={request_id:'job-1',workflow:{'1':{class_type:'EmptyImage'}}};
  assert.ok((await callSplit(send,'enqueue_workflow',args)).isError);
  const retried=await callSplit(send,'enqueue_workflow',args);
  assert.equal(JSON.parse(retried.content[0].text).prompt_id,'original');
  assert.ok((await callSplit(send,'enqueue_workflow',{...args,workflow:{other:1}})).isError);
  assert.deepEqual([commits,calls],[1,3]);
});

test('initializing, timed-out and failed CPU never receive a prompt',async t=>{
  let attempts=0;
  const slow=async path=>{
    assert.equal(path,'/split/startup');
    if(attempts++===0) return Response.json({ready:false,failed:false});
    throw new DOMException('timeout','TimeoutError');
  };
  const pending=await callSplit(slow,'enqueue_workflow',{});
  assert.equal(attempts,2);assert.match(pending.content[0].text,/starting/);
  const failed=await callSplit(async path=>{assert.equal(path,'/split/startup');return Response.json({ready:false,failed:true});},'enqueue_workflow',{});
  assert.match(failed.content[0].text,/initialization failed/);
});
