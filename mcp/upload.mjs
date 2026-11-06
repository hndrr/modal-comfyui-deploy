// Usage: node mcp/upload.mjs https://<split-host>/mcp file [file ...]
import {createHash,randomBytes} from 'node:crypto';
import {createServer} from 'node:http';
import {open} from 'node:fs/promises';
import {basename} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import {UnauthorizedError} from '@modelcontextprotocol/sdk/client/auth.js';

export async function uploadFile(path, call) {
  const f=await open(path,'r');
  try {
    const stat=await f.stat();
    if (!stat.isFile() || !stat.size || stat.size>256*1024**2) throw new Error('Expected a file of 1 byte to 256 MiB');
    const h=createHash('sha256');
    for await (const bytes of f.createReadStream({autoClose:false})) h.update(bytes);
    const digest=h.digest('hex'), filename=basename(path);
    const upload_id=createHash('sha256').update('cli:'+digest+':'+filename).digest('hex');
    let r=await call({upload_id,filename});
    const buf=Buffer.alloc(1024**2);
    while (!r.complete && r.next_offset<stat.size) {
      const offset=r.next_offset;
      const {bytesRead}=await f.read(buf,0,Math.min(buf.length,stat.size-offset),offset);
      if (!bytesRead) throw new Error('File changed during upload');
      r=await call({upload_id,offset,data:buf.subarray(0,bytesRead).toString('base64')});
      if (r.next_offset<=offset) throw new Error('Transfer did not advance');
    }
    if (!r.complete) r=await call({upload_id,total_bytes:stat.size});
    if (!r.complete || !r.filename?.startsWith('mcp-'+digest+'.')) throw new Error('Uploaded file did not match the source');
    return r.filename;
  } finally { await f.close(); }
}

async function connect(url) {
  const token=process.env.SPLIT_MCP_OAUTH_TOKEN;
  let server, provider, code, timer;
  if (!token) {
    const state=randomBytes(24).toString('hex');
    let receive;
    code=new Promise(resolve=>{receive=resolve});
    server=createServer((req,res)=>{
      const u=new URL(req.url,'http://127.0.0.1');
      if(req.method!=='GET'||u.pathname!=='/callback'||u.searchParams.get('state')!==state){
        res.writeHead(400).end();return;
      }
      res.writeHead(200,{'Content-Type':'text/plain','Cache-Control':'no-store'}).end('Return to the terminal.');
      receive(u.searchParams.get('code'));
    });
    await new Promise((ok,fail)=>{server.once('error',fail);server.listen(0,'127.0.0.1',ok)});
    const redirectUrl=new URL('http://127.0.0.1:'+server.address().port+'/callback');
    let info,tokens,verifier;
    provider={redirectUrl,clientMetadata:{client_name:'Split media upload',redirect_uris:[redirectUrl.href],
      grant_types:['authorization_code','refresh_token'],response_types:['code'],token_endpoint_auth_method:'none'},
      state:()=>state, clientInformation:()=>info,saveClientInformation:x=>{info=x},
      tokens:()=>tokens,saveTokens:x=>{tokens=x},codeVerifier:()=>verifier,saveCodeVerifier:x=>{verifier=x},
      redirectToAuthorization:u=>process.stderr.write('Authorize this upload in your browser:\n'+u.href+'\n')};
  }
  const make=()=>new StreamableHTTPClientTransport(url,token?{requestInit:{headers:{Authorization:'Bearer '+token}}}:{authProvider:provider});
  let transport=make(), client=new Client({name:'split-upload',version:'1'});
  try {
    try { await client.connect(transport); }
    catch(e) {
      if (!provider || !(e instanceof UnauthorizedError)) throw e;
      const authCode=await Promise.race([code,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Login timed out')),180000)})]);
      if (!authCode) throw new Error('Authorization declined');
      await transport.finishAuth(authCode);
      await transport.close();
      client=new Client({name:'split-upload',version:'1'});
      transport=make();
      await client.connect(transport);
    }
    return client;
  } finally { clearTimeout(timer); server?.close(); }
}

if(import.meta.main){
  const [endpoint,...files]=process.argv.slice(2);
  const url=new URL(endpoint);
  if(url.protocol!=='https:'||url.username||url.password||url.pathname!=='/mcp'||url.search||url.hash||!files.length)
    throw new Error('Usage: node mcp/upload.mjs https://<split-host>/mcp FILE [FILE...]');
  const client=await connect(url);
  try {
    for(const file of files) console.log(await uploadFile(file,async args=>{
      for(let attempt=0;;attempt++){
        const r=await client.callTool({name:'upload_media',arguments:args});
        const text=r.content?.[0]?.text || '';
        if (!r.isError) return JSON.parse(text);
        if (!/CPU is starting|Split transfer unavailable/.test(text) || attempt>=10) throw new Error(text);
        await delay(2000);
      }
    }));
  } finally { await client.close(); }
}
