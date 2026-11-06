// A review is reachable only with a verified owner browser session. Machine
// credentials may create a pending review, but never read or decide it.
const reviewPath = /^\/mcp-approvals\/[a-f0-9]{8}(?:\.[a-f0-9]{8}){3}$/;
export function receiptLink(receipt: {status?:string; approval_path?:string}, origin?:string) {
  if (!origin || receipt.status!=="approval_required" || !reviewPath.test(receipt.approval_path ?? ""))
    throw new Error("Invalid approval receipt");
  return new URL(receipt.approval_path!,origin).href;
}

export function serviceReadAllowed(path:string) {
  if (path==="/api/assets" || /^\/assets\/[\w.-]+\.(?:js|css)$/.test(path)) return true;
  return /^\/(?:api\/)?(?:ws|system_stats|prompt|queue|view|features|extensions|models(?:\/[\w.-]+)?|history(?:\/[\w-]+)?|object_info(?:\/[\w.-]+)?)$/.test(path);
}

export async function browserReview(request:Request, permitted:boolean, origin:string, headers:Headers) {
  if (!permitted) return new Response(null,{status:403});
  const url=new URL(request.url);
  if (url.search || !/^\/mcp-approvals\/[0-9a-f]{8}(?:\.[0-9a-f]{8}){3}(?:\/media\/[0-9]{1,3})?$/.test(url.pathname))
    return new Response(null,{status:404});
  if (request.method!=="GET" && request.method!=="POST") return new Response(null,{status:405});
  if (request.method==="POST" && (request.headers.get("Origin")!==url.origin ||
      request.headers.get("Content-Type")?.split(";")[0]!=="application/x-www-form-urlencoded"))
    return new Response(null,{status:403});
  headers.set("X-Split-Browser-Review","1");
  if (request.method==="POST") headers.set("Content-Type","application/x-www-form-urlencoded");
  if (request.headers.has("Range")) headers.set("Range",request.headers.get("Range")!);
  return fetch(new Request(origin+url.pathname.replace("/mcp-approvals/","/_remote_mcp/review/"),{
    method:request.method,headers,body:request.body,...(request.body?{duplex:"half"}:{}),redirect:"manual"}));
}
