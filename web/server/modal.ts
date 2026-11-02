// Only the proxy-authenticated Modal web function starts this listener.
import { serve } from "@hono/node-server";
import { createApp } from "./app.js";

serve({ hostname: "0.0.0.0", port: 7860, fetch: createApp({ modalProfile: async () => null }).fetch });
