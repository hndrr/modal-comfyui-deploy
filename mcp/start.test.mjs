import assert from 'node:assert/strict';
import { mkdtemp, writeFile, copyFile, symlink, rm, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

// Start the published package over stdio with a private fixture config. Only
// the final fetch is replaced; the real server must build the Access headers.
test('split MCP exposes five tools and queries remote nodes with Access', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'comfy-mcp-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const mcp = join(dir, 'mcp');
  await mkdir(mcp);
  await copyFile(new URL('../.node-version', import.meta.url), join(dir, '.node-version'));
  await copyFile(new URL('start.mjs', import.meta.url), join(mcp, 'start.mjs'));
  await symlink(new URL('node_modules', import.meta.url), join(mcp, 'node_modules'));
  await writeFile(join(dir, '.env.comfy-mcp'),
    'COMFYUI_URL=https://split.example.com\nCF_ACCESS_CLIENT_ID=test.access\nCF_ACCESS_CLIENT_SECRET=fixture\n', { mode: 0o600 });
  const mock = join(dir, 'fixture.mjs');
  await writeFile(mock, `
    globalThis.fetch = async (input, init) => {
      const req = new Request(input, init);
      if (req.url.split('?')[0] !== 'https://split.example.com/object_info' ||
          req.redirect !== 'error' || req.headers.get('CF-Access-Client-Id') !== 'test.access' ||
          req.headers.get('CF-Access-Client-Secret') !== 'fixture') throw new Error('unexpected request');
      return Response.json(Object.fromEntries(['EmptyImage','SaveImage'].map(name => [name,{
        name, display_name:name, description:'fixture', input:{required:{}}, output:[],
        output_name:[], output_is_list:[], category:'image', output_node:name==='SaveImage'
      }])));
    };
  `);
  const client = new Client({ name: 'split-test', version: '1' });
  const transport = new StdioClientTransport({ command: process.execPath, args: ['--import',mock,join(mcp,'start.mjs')],
    cwd: dir, stderr:'pipe', env: { PATH:process.env.PATH, COMFYUI_API_KEY:'must-not-enable-cloud' } });
  t.after(() => client.close());
  await client.connect(transport);
  const names = (await client.listTools()).tools.map(x => x.name).sort();
  assert.deepEqual(names, ['call_tool','create_workflow','describe_tool','enqueue_workflow','get_history','get_image','list_tools','queue']);
  for (const node_type of ['EmptyImage','SaveImage']) {
    const result = await client.callTool({ name:'create_workflow', arguments:{ action:'node_info', node_type } });
    assert.ok(!result.isError, JSON.stringify(result.content));
    assert.match(result.content[0].text, new RegExp(node_type));
    assert.doesNotMatch(result.content[0].text, /No nodes found/);
  }
  assert.ok((await client.callTool({ name:'queue', arguments:{ action:'clear' } })).isError);
});
