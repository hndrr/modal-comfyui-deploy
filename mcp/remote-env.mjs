// Only for the isolated remote tool process. No inherited provider credentials.
const origin = new URL(process.env.COMFYUI_URL).origin;
if (new URL(origin).hostname !== '127.0.0.1') throw new Error('Expected the local split gateway');
const nativeFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  let request = new Request(input, init);
  const target=new URL(request.url);
  if (process.env.SPLIT_MCP_REGISTRY==='1' && request.method==='GET' && target.origin==='https://api.comfy.org'
      && /^\/nodes(?:\/[a-zA-Z0-9_-]+)?$/.test(target.pathname)) {
    return nativeFetch(target,{method:'GET',redirect:'error',signal:request.signal});
  }
  if (target.origin !== origin) throw new Error('Unexpected upstream origin');
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    const url = new URL(request.url);
    const name=process.env.SPLIT_MCP_SAVE_NAME;
    if (!name || request.method!=='POST' || url.pathname!=='/api/userdata/'+encodeURIComponent('workflows/'+name)) throw new Error('Unexpected upstream write');
    url.search = '?overwrite=false';
    request = new Request(url, request);
  }
  request.headers.set('X-Modal-Execution-Mode', 'split');
  return nativeFetch(request, {redirect: 'error'}).then(res=>{
    if (request.method==='POST' && !res.ok) throw new Error('Workflow was not saved (HTTP '+res.status+'); choose a new filename if it already exists.');
    return res;
  });
};
