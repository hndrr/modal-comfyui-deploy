import {test} from 'node:test';
import assert from 'node:assert/strict';
import {scrubSecretShapedText} from './node_modules/comfyui-mcp/dist/comfyui/json-guard.js';
import {receiptLink} from '../worker/src/job-review.ts';

test('pinned stdio redaction preserves the public review address without exposing any credentials',()=>{
  const link=receiptLink({status:'approval_required',approval_path:'/mcp-approvals/abc12345.12345678.89abcdef.f0123456'},'https://split.example.com');
  assert.equal(scrubSecretShapedText('Owner approval required: '+link),'Owner approval required: '+link);
});
