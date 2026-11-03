import {test} from 'node:test';
import assert from 'node:assert/strict';
import {uploadMedia} from '../src/upload-media.ts';
const file={file_id:'file-123',download_url:'https://files.oaiusercontent.com/signed',file_name:'音.wav'};
test('attachment retries request only missing bytes, without origin credentials',async()=>{
  const actions=[];
  const send=async(path,init)=>{
    const q=new URL(path,'https://example.invalid').searchParams;
    actions.push(q.get('op'));
    assert.equal(init.headers['X-Split-Remote-Tool'],'1');
    if(q.get('op')==='part'){
      assert.equal(q.get('offset'),'4');assert.deepEqual([...init.body],[5,6,7,8]);
    }
    if(q.get('op')==='finish')assert.equal(q.get('total'),'8');
    return Response.json({next_offset:4,complete:q.get('op')==='finish'});
  };
  const r=await uploadMedia(send,{file},async(url,init)=>{
    assert.equal(url.href,file.download_url);
    assert.deepEqual(init.headers,{Range:'bytes=4-'});assert.equal(init.redirect,'manual');
    return new Response(new Uint8Array([5,6,7,8]),{status:206,headers:{'Content-Range':'bytes 4-7/8'}});
  });
  assert.ok(!r.isError);assert.deepEqual(actions,['status','part','finish']);
});
test('unapproved URLs, redirect, wrong resume range and oversized source are not transferred',async()=>{
  for(const download_url of ['http://files.oaiusercontent.com/x','https://evil.com/x','https://files.oaiusercontent.com.evil.com/x']){
    const r=await uploadMedia(()=>assert.fail(),{file:{...file,download_url}},()=>assert.fail());
    assert.ok(r.isError);
  }
  for(const res of [new Response(null,{status:302,headers:{Location:'https://evil.com/'}}),
    new Response('xx',{status:206,headers:{'Content-Range':'bytes 0-1/2'}}),
    new Response('xx',{headers:{'Content-Length':String(256*1024**2+1)}})]){
    const r=await uploadMedia(async p=>{
      assert.match(p,/op=status/);return Response.json({next_offset:4});
    },{file},async()=>res);
    assert.ok(r.isError);
  }
});

test('Azure requires the exact configured attachment account and a signed URL', async()=>{
  const host='fixturecdn.blob.core.windows.net', allowed=host+',secondfixture.blob.core.windows.net';
  for(const [url,hosts,ok] of [
    ['https://'+host+'/f?sig=test',allowed,true],
    ['https://'+host+'/f?sig=test','',false], ['https://'+host+'/f?sig=test','*.blob.core.windows.net',false],
    ['https://otherfixture.blob.core.windows.net/f?sig=test',allowed,false],
    ['https://'+host+'/f',allowed,false], ['http://'+host+'/f?sig=test',allowed,false],
    ['https://'+host+'.evil.com/f?sig=test',allowed,false],
  ]) {
    let contacted=false;
    const r=await uploadMedia(async()=>Response.json({complete:false,next_offset:0}),{file:{...file,download_url:url}},async(_u,init)=>{
      contacted=true;assert.deepEqual(init.headers,{});assert.equal(init.redirect,'manual');
      return new Response(new Uint8Array([1]));
    },hosts);
    assert.equal(contacted,ok);assert.equal(!r.isError,ok);
  }
});
