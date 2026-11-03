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
  for (const name of ["get_image", "get_video", "get_audio", "upload_media", "get_workflow", "cancel_job", "get_job_progress", "list_models","calculate","get_split_status","save_workflow","visualize_workflow","list_nodes","search_custom_nodes","get_job_workflow","reuse_output","list_inputs","list_outputs","inspect_media","prepare_environment","change_node","apply_environment","discard_environment","add_model","inspect_model","remove_model","restore_model","get_admin_operation"])
    assert.ok(list.result.tools.some(t => t.name === name));
  const upload=list.result.tools.find(t=>t.name==='upload_media');
  const audio=list.result.tools.find(t=>t.name==="get_audio"),
    part=list.result.tools.find(t=>t.name==="read_audio_part");
  assert.deepEqual(part._meta.ui.visibility,["app"]);
  assert.ok(audio.outputSchema.properties.delivery && part.outputSchema.properties.size);
  const resource=await (await request(jwt,"resources/read",{uri:audio._meta.ui.resourceUri})).json();
  assert.match(resource.result.contents[0].mimeType,/profile=mcp-app/);
  assert.match(resource.result.contents[0].text,/<audio /);
  assert.deepEqual(upload._meta['openai/fileParams'],['file']);
  assert.deepEqual(upload.inputSchema.properties.file.required.sort(),['download_url','file_id']);
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
  assert.deepEqual((await discovery.json()).result.tools.map(t=>t.name).sort(),
    list.result.tools.map(t=>t.name).sort());
  assert.equal(hits.length,1);

  const document=token=>worker.fetch(new Request("https://"+host+"/view?type=output&filename=test.wav",{
    headers:{"CF-Access-Jwt-Assertion":token,"Sec-Fetch-Dest":"document","Sec-Fetch-Mode":"navigate","Sec-Fetch-Site":"cross-site"},
  }),shared);
  assert.equal((await document("")).status,403);
  assert.equal((await document(jwt)).status,403);
  assert.match(await (await document(browser)).text(),/<audio controls/);
  assert.equal(hits.length,1); // Rendering the signed-in page never touches Modal.

  // Other ComfyUI hosts may already have custom-node routes below /mcp.
  const other = "ordinary.example.com", upstream = "https://ordinary.modal.run";
  for (const binding of [shared.SPLIT_REMOTE_MCP, undefined]) {
    const env = {...shared, MODAL_ORIGINS:JSON.stringify({[other]:upstream}), SPLIT_REMOTE_MCP:binding};
    for (const path of ["/mcp", "/mcp/panel"]) {
      const response = await worker.fetch(new Request("https://"+other+path, {
        headers:{"CF-Access-Jwt-Assertion":browser}}),env);
      assert.equal(response.status,200);
      assert.equal(hits.at(-1).url,upstream+path);
    }
  }
});

test('a lost receipt uses exactly the same queue request on retry',async()=>{
  const receipts=new Map();let commits=0,calls=0;
  const send=async(path,init)=>{
    if(path==='/split/startup')return Response.json({ready:true});
    assert.equal(path,'/_remote_mcp/submit');assert.equal(init.headers['X-Split-Remote-Tool'],'1'); const key=init.headers['Idempotency-Key'];
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

test('409 reports a bounded gateway reason or a neutral rejection', async()=>{
  for (const [body, expected] of [
    [JSON.stringify({error:"Enable the integration before submitting these nodes."}), " Enable the integration before submitting these nodes."],
    [JSON.stringify({error:"a".repeat(1000)}), " "+"a".repeat(300)],
    [JSON.stringify({error:["invalid"]}), ""], ["upstream error page", ""],
    [JSON.stringify({error:"x".repeat(9000)}), ""],
  ]) {
    const r=await callSplit(async path => path==="/split/startup" ? Response.json({ready:true}) :
      new Response(body,{status:409}),"enqueue_workflow",{request_id:"key",workflow:{}});
    assert.equal(r.isError,true);
    assert.equal(r.content[0].text,"Request rejected with HTTP 409."+expected);
  }
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

test('model inventory scopes reads to one category, filters and paginates filenames',async()=>{
  const paths=[];
  const send=async p=>{paths.push(p);return Response.json(p==='/split/startup'?{ready:true}:
    p==='/models'?['loras','checkpoints']:['sub/B.safetensors','a.safetensors','skip.pt']);};
  const call=async args=>JSON.parse((await callSplit(send,'list_models',args)).content[0].text);
  assert.deepEqual((await call({})).categories,['checkpoints','loras']);
  const first=await call({category:'loras',query:'.SAFE',limit:1});
  assert.deepEqual(first.models,['a.safetensors']);assert.equal(first.next_offset,1);
  assert.deepEqual((await call({category:'loras',query:'.SAFE',offset:1})).models,['sub/B.safetensors']);
  const bad=await callSplit(send,'list_models',{category:'../queue'});
  assert.ok(bad.isError);
  assert.ok(paths.every(p=>['/split/startup','/models','/models/loras'].includes(p)));
});

test('management and production perform only the named operation after readiness',async()=>{
  const hits=[];
  const send=async(path,opts)=>{
    hits.push(path);
    if(path==='/split/startup')return Response.json({ready:true});
    assert.equal(opts.headers['X-Split-Remote-Tool'],'1');
    assert.equal(JSON.parse(opts.body).name,'change_node');
    return Response.json({status:'failed',error:'Candidate version changed.'});
  };
  const result=await callSplit(send,'change_node',{candidate:'old',request_id:'test',action:'install',node:'example'});
  assert.deepEqual(hits,['/split/startup','/_remote_mcp/operations']);
  assert.equal(result.isError,true);
  assert.match(result.content[0].text,/version changed/);
});

test('an external submission advertises only its trusted browser link, never a prompt id',async()=>{
  const args={request_id:'music',workflow:{'1':{}}};
  const path='/mcp-approvals/'+Array(4).fill('a'.repeat(8)).join('.');
  for(const approval_path of [path,'//different.example/steal',path+'/media/0']){
    const send=async p=>{
      if(p==='/split/startup')return Response.json({ready:true});
      assert.equal(p,'/_remote_mcp/submit');
      return Response.json({status:'approval_required',approval_path,expires_at:1500},{status:202});
    };
    const r=await callSplit(send,'enqueue_workflow',args,'https://split.example.com');
    if(approval_path===path){
      const info=JSON.parse(r.content[0].text);
      assert.equal(info.approval_url,'https://split.example.com'+path);
      assert.equal(info.prompt_id,undefined);
    }else assert.ok(r.isError);
  }
});
