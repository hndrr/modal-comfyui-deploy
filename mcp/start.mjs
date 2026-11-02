import { readFileSync, lstatSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parseEnv } from 'node:util';

const path = name => fileURLToPath(new URL(name, import.meta.url));
const pin = readFileSync(path('../.node-version'), 'utf8').trim();
if (process.versions.node !== pin) throw new Error('comfyui-split requires Node.js ' + pin);

const file = path('../.env.comfy-mcp');
const stat = lstatSync(file);
if (!stat.isFile() || (stat.mode & 0o077)) throw new Error('Set .env.comfy-mcp permissions to 600');
const config = parseEnv(readFileSync(file, 'utf8'));
const keys = ['COMFYUI_URL', 'CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET'];
if (keys.some(k => !config[k]?.trim()) || Object.keys(config).some(k => !keys.includes(k)))
  throw new Error('Set only the three required fields in .env.comfy-mcp');
const origin = URL.parse(config.COMFYUI_URL)?.origin;
if (!origin || !origin.startsWith('https://') || ![origin, origin + '/'].includes(config.COMFYUI_URL) ||
    new URL(origin).hostname.endsWith('.modal.run'))
  throw new Error('COMFYUI_URL must be the HTTPS origin of the split Access hostname');

// A Codex process may also have credentials for other ComfyUI installations.
for (const k of Object.keys(process.env))
  if (!['HOME', 'PATH', 'TMPDIR', 'TEMP', 'TMP', 'SystemRoot', 'LANG'].includes(k)) delete process.env[k];
Object.assign(process.env, {
  COMFYUI_MCP_ENV_FILE: file,
  COMFYUI_MCP_DATA_DIR: path('.state'),
  COMFYUI_MCP_TOOL_MODE: 'full',
  COMFYUI_MCP_PANEL_AUTOINSTALL: '0',
  COMFYUI_MCP_AUTO_UPDATE_DISABLE: '1',
  COMFYUI_MCP_TOOL_ALLOW: 'create_workflow,enqueue_workflow,queue,get_history,get_image',
  COMFYUI_MCP_TOOL_ACTION_ALLOW: [
    'create_workflow:create', 'create_workflow:modify', 'create_workflow:validate', 'create_workflow:node_info',
    'enqueue_workflow:enqueue', 'queue:list', 'queue:status', 'get_history:list', 'get_history:diagnose', 'get_image:get',
  ].join(','),
});
// Nonstandard Access headers are not stripped by fetch on a cross-origin redirect.
const nativeFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : input);
  if (url.origin !== origin) throw new Error('comfyui-split refused a different origin');
  return nativeFetch(input, { ...init, redirect: 'error' });
};
await import('comfyui-mcp');
