import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

import policy from "./surface.json" with {type:"json"};
import {libraryName, libraryRequest} from './library.mjs';

const actions=Object.fromEntries(Object.entries(policy).map(([name, rows]) =>
  [name, rows.filter(r=>r.mode==='direct').flatMap(r=>r.actions)]).filter(([,list])=>list.length));
// Compatibility when the separate saved-data endpoint is not configured.
// These remain server-side calls; clients only connect to HTTPS /mcp.
actions.get_history=['list','diagnose'];
export async function invoke({name, arguments: args}, dir, origin = 'http://127.0.0.1:8000') {
  if (!Object.hasOwn(actions, name) || !actions[name].includes(args?.action ?? "(call)")) throw new Error('Unsupported action');
  if (name === 'get_workflow' && !libraryRequest(args)) throw new Error('Invalid workflow library request');
  if (name === 'save_workflow' && (!libraryName(args.filename) || !args.workflow || Object.keys(args).some(k=>!['action','filename','workflow'].includes(k)))) throw new Error('Invalid workflow save');
  const client = new Client({name:'split-cpu',version:'1'});
  const transport = new StdioClientTransport({
    command:process.execPath, cwd:dir, stderr:'ignore',
    args:['--import',fileURLToPath(new URL('remote-env.mjs',import.meta.url)),
      fileURLToPath(import.meta.resolve('comfyui-mcp'))],
    env:{
      HOME:dir, SPLIT_MCP_REGISTRY:name==='search_custom_nodes'?'1':'', PATH:process.env.PATH, COMFYUI_URL:origin, SPLIT_MCP_SAVE_NAME:name==='save_workflow'?args.filename:'', COMFYUI_MCP_FORCE_REMOTE:'1',
      COMFYUI_MCP_DATA_DIR:join(dir,'data'), COMFYUI_MCP_PANEL_AUTOINSTALL:'0',
      COMFYUI_MCP_AUTO_UPDATE_DISABLE:'1', COMFYUI_MCP_TOOL_MODE:'full',
      COMFYUI_MCP_TOOL_ALLOW:Object.keys(actions).join(','),
      COMFYUI_MCP_TOOL_ACTION_ALLOW:Object.entries(actions).flatMap(([n, list])=>list.filter(a=>a!=='(call)').map(a=>n+':'+a)).join(','),
    },
  });
  try {
    await client.connect(transport);
    const r = await client.callTool({name,arguments:args});
    return {content:r.content, isError:r.isError};
  } finally { await client.close(); }
}
if (import.meta.main) {
  const data=[];
  for await (const chunk of process.stdin) data.push(chunk);
  const result = await invoke(JSON.parse(Buffer.concat(data)), process.cwd());
  process.stdout.write(JSON.stringify(result));
}
