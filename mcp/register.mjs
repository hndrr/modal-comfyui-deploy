// Add a checkout-local server. Credentials never appear in Codex configuration.
import { appendFileSync, mkdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const pin = readFileSync(new URL('../.node-version', import.meta.url), 'utf8').trim();
if (process.versions.node !== pin) throw new Error('Run with Node.js ' + pin);
const file = new URL('../.codex/config.toml', import.meta.url);
const section = '[mcp_servers.comfyui-split]';
const block = [section, 'command = ' + JSON.stringify(process.execPath),
  'args = [' + JSON.stringify(fileURLToPath(new URL('start.mjs', import.meta.url))) + ']',
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
