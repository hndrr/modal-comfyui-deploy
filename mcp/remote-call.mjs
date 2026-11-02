import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const actions = {
  create_workflow: ['create', 'modify', 'validate', 'node_info'],
  queue: ['list', 'status'], get_history: ['list', 'diagnose'], get_image: ['get'], get_workflow: ['list', 'get'],
};
export async function invoke({name, arguments: args}, dir, origin = 'http://127.0.0.1:8000') {
  if (!Object.hasOwn(actions, name) || !actions[name].includes(args?.action)) throw new Error('Unsupported action');
  if (name === 'get_workflow') {
    const file = args.filename;
    if (Object.keys(args).some(k => !['action','filename','format'].includes(k)) ||
        (args.action === 'list' ? file !== undefined || args.format !== undefined :
          typeof file !== 'string' || file.length > 512 || !file.endsWith('.json') ||
          /[\\:\x00-\x1f\x7f]|%(?:2e|2f|5c|25)/i.test(file) || file.split('/').some(p=>!p || p==='.' || p==='..') ||
          (args.format !== undefined && !['api','ui'].includes(args.format)))) throw new Error('Invalid workflow library request');
  }
  if (name === 'get_image' && ('save_dir' in args || args.type && args.type !== 'output')) throw new Error('Output images only');
  const client = new Client({name:'split-cpu',version:'1'});
  const transport = new StdioClientTransport({
    command:process.execPath, cwd:dir, stderr:'ignore',
    args:['--import',fileURLToPath(new URL('remote-env.mjs',import.meta.url)),
      fileURLToPath(import.meta.resolve('comfyui-mcp'))],
    env:{
      HOME:dir, PATH:process.env.PATH, COMFYUI_URL:origin, COMFYUI_MCP_FORCE_REMOTE:'1',
      COMFYUI_MCP_DATA_DIR:join(dir,'data'), COMFYUI_MCP_PANEL_AUTOINSTALL:'0',
      COMFYUI_MCP_AUTO_UPDATE_DISABLE:'1', COMFYUI_MCP_TOOL_MODE:'full',
      COMFYUI_MCP_TOOL_ALLOW:Object.keys(actions).join(','),
      COMFYUI_MCP_TOOL_ACTION_ALLOW:Object.entries(actions).flatMap(([n, list])=>list.map(a=>n+':'+a)).join(','),
    },
  });
  try {
    await client.connect(transport);
    const r = await client.callTool({name,arguments:name === 'get_image' ?
      {...args, type:'output', save_dir:dir, max_preview_bytes:4*1024*1024} : args});
    if (name !== 'get_image') return {content:r.content, isError:r.isError};
    const images = r.content.filter(x=>x.type==='image');
    return images.length && !r.isError ? {content:images} :
      {isError:true, content:[{type:'text',text:'The output could not be returned as an inline image.'}]};
  } finally { await client.close(); }
}
if (import.meta.main) {
  const data=[];
  for await (const chunk of process.stdin) data.push(chunk);
  const result = await invoke(JSON.parse(Buffer.concat(data)), process.cwd());
  process.stdout.write(JSON.stringify(result));
}
