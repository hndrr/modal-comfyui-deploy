// Add a checkout-local server. Credentials never appear in Codex configuration.
import { appendFileSync, mkdirSync, readFileSync } from 'node:fs';
import { launch, serverName } from './client-config.mjs';

const file = new URL('../.codex/config.toml', import.meta.url);
const section = '[mcp_servers.' + serverName + ']';
const block = [section, 'command = ' + JSON.stringify(launch.command),
  'args = ' + JSON.stringify(launch.args),
  'enabled_tools = ["create_workflow", "enqueue_workflow", "get_history", "get_image", "queue"]',
  'startup_timeout_sec = 60', 'tool_timeout_sec = 180', ''].join('\n');
let existing = '';
try { existing = readFileSync(file, 'utf8'); } catch (e) { if (e.code !== 'ENOENT') throw e; }
if (!existing.includes(block)) {
  if (existing.includes(section)) throw new Error('Existing comfyui-split entry differs; inspect .codex/config.toml');
  mkdirSync(new URL('../.codex/', import.meta.url), { recursive: true });
  appendFileSync(file, '\n' + block, { mode: 0o600 });
}
console.log('Registered comfyui-split for this checkout.');
