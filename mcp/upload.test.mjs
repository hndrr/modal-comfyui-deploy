import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {createHash} from 'node:crypto';
import {uploadFile} from './upload.mjs';

test('CLI resumes a dropped reply with the same ID and sends only the remaining file bytes',async t=>{
  const dir=await mkdtemp(join(tmpdir(),'split-upload-'));
  t.after(()=>rm(dir,{recursive:true,force:true}));
  const file=join(dir,'音源.wav'), bytes=Buffer.alloc(1024**2+17,42);
  await writeFile(file,bytes);
  const digest=createHash('sha256').update(bytes).digest('hex');
  let id,offset=0,drop=true;
  const remote=async a=>{
    id??=a.upload_id;
    assert.equal(a.upload_id,id);
    if(a.filename!==undefined)return {complete:false,next_offset:offset};
    if(a.data!==undefined){
      assert.equal(a.offset,offset);
      const b=Buffer.from(a.data,'base64');
      assert.deepEqual(bytes.subarray(offset,offset+b.length),b);
      offset+=b.length;
      if(drop){drop=false;throw new Error('lost response')}
      return {complete:false,next_offset:offset};
    }
    assert.equal(a.total_bytes,bytes.length);
    return {complete:true,next_offset:offset,filename:'mcp-'+digest+'.wav'};
  };
  await assert.rejects(uploadFile(file,remote),/lost response/);
  assert.equal(await uploadFile(file,remote),'mcp-'+digest+'.wav');
});
