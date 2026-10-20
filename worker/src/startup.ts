/** Opt-in handling for Modal Servers' zero-to-one 503 responses. */
export function startupOrigins(raw: string | undefined, configured: Iterable<string>): Set<string> {
  if (!raw?.trim()) return new Set();
  const values: unknown = JSON.parse(raw);
  const allowed = new Set(configured);
  if (!Array.isArray(values) || values.some((value) => typeof value !== "string" || !allowed.has(value))) {
    throw new Error("MODAL_STARTUP_ORIGINS must list configured Modal origins");
  }
  return new Set(values as string[]);
}

const PAGE = `<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ComfyUIを起動しています</title>
<style>body{font:16px system-ui;max-width:36rem;margin:15vh auto;padding:1.5rem;line-height:1.8}button{font:inherit;padding:.5rem 1rem}</style>
<h1>ComfyUIを起動しています</h1><p id="status">準備ができると画面を開きます。</p>
<button id="retry" hidden>再読み込み</button>
<script>
const status=document.getElementById('status'), retry=document.getElementById('retry');
retry.onclick=()=>location.reload();
const deadline=Date.now()+600000;
function stop(message){status.textContent=message;retry.hidden=false;}
async function poll(){
 if(Date.now()>=deadline){stop('起動の確認がタイムアウトしました。');return;}
 try{
  const response=await fetch('/split/startup',{cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(15000)});
  if(response.status===401||response.status===403){stop('ログイン状態を確認して、再読み込みしてください。');return;}
  if(response.ok){
   const data=await response.json();
   if(data.failed){stop('ComfyUIの起動に失敗しました。');return;}
   if(data.ready){location.reload();return;}
  }
 }catch{}
 setTimeout(poll,2000);
}
poll();
</script></html>`;

export function startupResponse(request: Request, upstream: Response, enabled: boolean): Response {
  if (!enabled || upstream.status !== 503 || request.method !== "GET" ||
      !request.headers.get("Accept")?.includes("text/html") ||
      request.headers.get("Upgrade")?.toLowerCase() === "websocket") return upstream;
  // Do not retry or retain a request body. Only this page polls safe GETs.
  void upstream.body?.cancel();
  return new Response(PAGE, { status: 503, headers: {
    "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Retry-After": "2",
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'",
  } });
}
