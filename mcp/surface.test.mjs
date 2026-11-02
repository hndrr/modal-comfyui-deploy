import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {guide} from './surface-doc.mjs';
import policy from './surface.json' with {type:'json'};

test('every pinned upstream tool/action has exactly one split decision; the published guide matches', async t=>{
  const home=await mkdtemp(join(tmpdir(),'split-catalog-'));
  t.after(()=>rm(home,{recursive:true,force:true}));
  process.env.COMFYUI_WORKFLOWS_DIR=join(home,"workflows");
  process.env.COMFYUI_MCP_DATA_DIR=home;
  process.env.COMFYUI_MCP_PANEL_AUTOINSTALL='0';
  process.env.COMFYUI_MCP_AUTO_UPDATE_DISABLE='1';
  process.env.COMFYUI_URL='http://127.0.0.1:1';
  const {listRegisteredTools}=await import(new URL('./tools/introspect.js',import.meta.resolve('comfyui-mcp')));
  const tools=await listRegisteredTools('served');
  assert.deepEqual(Object.keys(policy).sort(),tools.map(t=>t.name).sort());
  for(const {name,inputSchema} of tools) {
    const actions=policy[name].flatMap(r=>{
      assert.ok(['direct','split','deferred'].includes(r.mode) && r.note.length,name);
      return r.actions;
    });
    assert.deepEqual(actions.sort(),(inputSchema.properties?.action?.enum??['(call)']).slice().sort(),name);
  }
  assert.equal(await readFile(new URL('../docs/split-mcp-tools.md',import.meta.url),'utf8'),guide);
});
