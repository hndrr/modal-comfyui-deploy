import { serve } from "@hono/node-server";
import { createApp } from "./app.js";
import { assertLoopbackHost } from "./lib/host.js";

const host = process.env.HOST ?? "127.0.0.1";
const port = Number(process.env.PORT ?? process.env.SERVER_PORT ?? "7860");

assertLoopbackHost(host);
const app = createApp();

console.log(`Modal ComfyUI Asset Manager (Hono + React)`);
console.log(`Listening on http://${host}:${port}`);
console.log(`API health: http://${host}:${port}/api/health`);

serve({
  fetch: app.fetch,
  hostname: host,
  port,
});
