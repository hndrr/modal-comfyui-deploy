import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// Use absolute paths so a stdio client can start the server from any directory.
const pin = readFileSync(new URL('../.node-version', import.meta.url), 'utf8').trim();
if (process.versions.node !== pin) throw new Error('Run with Node.js ' + pin);
export const serverName = 'comfyui-split';
export const launch = {
  command: process.execPath,
  args: [fileURLToPath(new URL('start.mjs', import.meta.url))],
};

if (import.meta.main) {
  console.log(JSON.stringify({ mcpServers: { [serverName]: launch } }, null, 2));
}
