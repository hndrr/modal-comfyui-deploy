// Only for the isolated remote tool process. No inherited provider credentials.
const origin = new URL(process.env.COMFYUI_URL).origin;
if (new URL(origin).hostname !== '127.0.0.1') throw new Error('Expected the local split gateway');
const nativeFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const request = new Request(input, init);
  if (new URL(request.url).origin !== origin) throw new Error('Unexpected upstream origin');
  request.headers.set('X-Modal-Execution-Mode', 'split');
  return nativeFetch(request, {redirect: 'error'});
};
