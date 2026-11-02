// Only these entry documents and the private UI subtrees belong to the GUIs.
// ComfyUI also serves GET /models (JSON), /models/<folder> and /assets/*.js.
type Gui = "models" | "assets";
type Binding = { hostname: string; owner_email: string; models_origin: string; assets_origin: string;
  key: string; secret: string };

export async function splitGui(request: Request, raw: string | undefined, splitHost: boolean,
  browserEmail: string | undefined): Promise<Response | null> {
  if (!raw || !splitHost) return null;
  const url = new URL(request.url);
  const match = /^\/(models|assets)(?:\/?$|\/_ui(\/.*)?$)/.exec(url.pathname);
  if (!match) return null;
  const gui = match[1] as Gui;
  const entry = !url.pathname.startsWith('/' + gui + '/_ui');
  if (entry && (!["GET", "HEAD"].includes(request.method) ||
      !request.headers.get("Accept")?.split(",").some(t => t.trim().split(";")[0] === "text/html"))) return null;

  const deny = (status: number) => new Response(null, { status, headers: { "Cache-Control": "no-store" } });
  let cfg: Binding, target: URL;
  try {
    cfg = JSON.parse(raw);
    target = new URL(cfg[gui === "models" ? "models_origin" : "assets_origin"]);
    if (cfg.hostname !== url.hostname || !cfg.owner_email || !cfg.key || !cfg.secret ||
        target.protocol !== "https:" || !target.hostname.endsWith(".modal.run") || target.href !== target.origin + "/")
      return deny(503);
  } catch { return deny(503); }
  if (!browserEmail || cfg.owner_email.toLowerCase() !== browserEmail.toLowerCase()) return deny(403);
  const origin = request.headers.get("Origin");
  if ((origin && origin !== url.origin) || (!["GET","HEAD"].includes(request.method) && origin !== url.origin))
    return deny(403);

  if (entry && gui === "models") {
    // The iframe keeps the address short while Gradio resolves relative JS,
    // styles and queue URLs below its own directory.
    return new Response(request.method === "HEAD" ? null : `<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>モデル保存</title>
<style>body{margin:0}iframe{border:0;width:100%;height:100dvh;display:block}</style>
<iframe title="モデル保存" src="/models/_ui/"></iframe></html>`,
      { headers: { "Content-Type":"text/html; charset=utf-8", "Cache-Control":"no-store", "Vary":"Accept" } });
  }
  const base = "/" + gui + "/_ui";
  if (url.pathname === base) return Response.redirect(url.origin + base + "/" + url.search, 308);
  // Assign pathname on the fixed origin; a tail beginning with // can never select another host.
  target.pathname = entry ? "/" : url.pathname.slice(base.length);
  target.search = url.search;
  // Hono decodes unreserved characters before matching its content route.
  // Classify the same path without rewriting the URL sent to Modal.
  let route: string;
  try { route = decodeURI(target.pathname); } catch { return deny(400); }
  const headers = new Headers({ "Modal-Key": cfg.key, "Modal-Secret": cfg.secret });
  for (const name of ["Accept","Content-Type","Range","If-None-Match","If-Modified-Since","Last-Event-ID"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name,value);
  }
  const upstream = await fetch(new Request(target, { method: request.method, headers,
    body: request.body, redirect:"manual" }));
  const downstream = new Headers(upstream.headers);
  downstream.delete("Set-Cookie");
  downstream.set("Cache-Control","private, no-store");
  downstream.set("Referrer-Policy","same-origin");
  downstream.set("Vary","Accept");
  const location = downstream.get("Location");
  if (location) {
    const redirect = new URL(location, target);
    if (redirect.origin !== target.origin) return deny(502);
    downstream.set("Location", base + redirect.pathname + redirect.search + redirect.hash);
  }
  if (gui === "assets" && route === "/api/assets/content") {
    downstream.set("Content-Security-Policy", "sandbox; default-src 'none'");
    const type = downstream.get("Content-Type")?.split(";")[0] || "";
    // Even an attachment can be loaded as a script by another page on this origin.
    if (!/^(image\/(png|jpeg|gif|webp|bmp)|video\/[\w.+-]+|audio\/[\w.+-]+)$/i.test(type)) {
      downstream.set("Content-Type","application/octet-stream");
      downstream.set("X-Content-Type-Options","nosniff");
    }
  }
  return new Response(upstream.body, { status:upstream.status, headers:downstream });
}
