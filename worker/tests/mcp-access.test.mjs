import assert from "node:assert/strict";
import { test } from "node:test";
import { exportJWK, generateKeyPair, SignJWT } from "jose";
import worker from "../src/index.ts";

test("signed Access identity controls both transports before contacting an origin", async (t) => {
  const team = "https://team.cloudflareaccess.com";
  const split = "split.example.com";
  const other = "models.example.com";
  const clientId = "test-client.access";
  const binding = JSON.stringify({ client_id: clientId, hostname: split, origin: "https://split.modal.run" });
  const env = {
    MODAL_ORIGINS: JSON.stringify({
      [split]: "https://split.modal.run", [other]: "https://models.modal.run",
    }),
    TEAM_DOMAIN: team, POLICY_AUD: "aud", MODAL_KEY: "modal-id", MODAL_SECRET: "modal-secret",
    SPLIT_MCP_ACCESS: binding,
  };
  const keys = await generateKeyPair("RS256");
  const jwk = { ...await exportJWK(keys.publicKey), kid: "test" };
  const sign = (claims, key = keys.privateKey) => new SignJWT({ type: "app", ...claims })
    .setProtectedHeader({ alg: "RS256", kid: jwk.kid }).setIssuer(team)
    .setAudience("aud").setExpirationTime("5m").sign(key);
  const machine = await sign({ sub: "", common_name: clientId });
  const browser = await sign({ sub: "browser-user" });
  const forwarded = [];
  t.mock.method(globalThis, "fetch", async (request) => {
    if (request === team + "/cdn-cgi/access/certs") return Response.json({ keys: [jwk] });
    forwarded.push(request);
    // Node fetch does not construct a 101 Response. Sentinels exercise the
    // Worker's real WS dispatch and verify the upstream response is passed through.
    return request.headers.get("Upgrade") ? { status: 101, webSocket: {} } : new Response("ok");
  });
  const call = (host, jwt, websocket = false, config = env) => worker.fetch(new Request(
    "https://" + host + (websocket ? "/ws?clientId=test" : "/object_info"), {
      headers: {
        "CF-Access-Jwt-Assertion": jwt, "CF-Access-Client-Id": clientId,
        "CF-Access-Client-Secret": "edge-only", "CF-Access-Token": "alternate",
        Cookie: "CF_Authorization=private; CF_Binding=private; CF_AppSession=private; other=ok",
        ...(websocket ? { Upgrade: "websocket" } : {}),
      },
    }), config);
  for (const ws of [false, true]) {
    const before = forwarded.length;
    assert.equal((await call(other, machine, ws)).status, 403);
    assert.equal((await call(split, await sign({ sub: "", common_name: "other.access" }), ws)).status, 403);
    assert.equal((await call(split, machine, ws, { ...env, SPLIT_MCP_ACCESS: undefined })).status, 403);
    assert.equal((await call(split, machine, ws, { ...env, MODAL_ORIGINS: JSON.stringify({
      [split]: "https://different.modal.run",
    }) })).status, 403);
    assert.equal(forwarded.length, before);
    assert.equal((await call(split, machine, ws)).status, ws ? 101 : 200);
    const up = forwarded.at(-1);
    assert.equal(new URL(up.url).hostname, "split.modal.run");
    for (const name of ["CF-Access-Jwt-Assertion", "CF-Access-Client-Id", "CF-Access-Client-Secret", "CF-Access-Token"]) {
      assert.equal(up.headers.get(name), null);
    }
    assert.equal(up.headers.get("Cookie"), "other=ok");
    assert.equal(up.headers.get("Modal-Secret"), env.MODAL_SECRET);
    assert.equal(up.headers.get("X-Modal-Execution-Mode"), "split");
    assert.equal((await call(other, browser, ws)).status, ws ? 101 : 200);
    assert.equal(forwarded.at(-1).headers.get("X-Modal-Execution-Mode"), null);
  }
  const forged = await sign({ sub: "", common_name: clientId }, (await generateKeyPair("RS256")).privateKey);
  const before = forwarded.length;
  for (const token of [forged, await sign({ sub: "" }), await sign({ sub: "user", common_name: clientId })]) {
    assert.equal((await call(split, token)).status, 403);
  }
  assert.equal(forwarded.length, before);
});

test('only the signed UI owner may view/decide one review; service POSTs cannot bypass admission', async t=>{
  const team="https://review-test.cloudflareaccess.com", host="split.example.com", origin="https://split.modal.run";
  const env={TEAM_DOMAIN:team,POLICY_AUD:"browser",MODAL_KEY:"key",MODAL_SECRET:"secret",
    MODAL_ORIGINS:JSON.stringify({[host]:origin}),
    SPLIT_MCP_ACCESS:JSON.stringify({client_id:"client.access",hostname:host,origin}),
    SPLIT_REMOTE_MCP:JSON.stringify({hostname:host,split_hostname:host,origin,audience:"oauth",owner_email:"owner@example.com"})};
  const {privateKey,publicKey}=await generateKeyPair("RS256");
  const jwk={...await exportJWK(publicKey),kid:"review"};
  const sign=(claims,aud)=>new SignJWT({type:"app",...claims}).setProtectedHeader({alg:"RS256",kid:jwk.kid})
    .setIssuer(team).setAudience(aud).setExpirationTime("5m").sign(privateKey);
  const owner=await sign({sub:"user",email:"owner@example.com"},"browser"),
    oauth=await sign({sub:"user",email:"owner@example.com"},"oauth"),other=await sign({sub:"user",email:"another@example.com"},"browser"),
    service=await sign({sub:"",common_name:"client.access"},"browser");
  const path="/mcp-approvals/"+["a".repeat(8),"b".repeat(8),"c".repeat(8),"d".repeat(8)].join("."), internal=path.replace("/mcp-approvals/","/_remote_mcp/review/");
  const hits=[];
  t.mock.method(globalThis,"fetch",async r=>{
    if(r===team+"/cdn-cgi/access/certs")return Response.json({keys:[jwk]});
    hits.push(r);
    if(r.url.endsWith("/_remote_mcp/submit"))return Response.json({status:"approval_required",approval_path:path},{status:202});
    return new Response("ok");
  });
  const call=(p,token,method="GET",extra={})=>worker.fetch(new Request("https://"+host+p,{method,headers:{
    "CF-Access-Jwt-Assertion":token,"X-Split-Browser-Review":"1","X-Split-Remote-Tool":"1",
    Cookie:"CF_Authorization=private; extra=private",Authorization:"Bearer secret",...extra},
    ...(method==="POST"?{body:"x=1"}:{})}),env);
  for(const token of [oauth,other,service,""]){assert.equal((await call(path,token)).status,403);}
  for(const p of [path,internal,"/api"+internal,"/api/split/mode","/api/prompt/","/api/%70rompt","/prompt?alternate=1"])
    assert.equal((await call(p,service,"POST")).status,403);
  for(const headers of [{},{"Origin":"https://other.example","Content-Type":"application/x-www-form-urlencoded"},
    {"Origin":"https://"+host,"Content-Type":"application/json"}])assert.equal((await call(path,owner,"POST",headers)).status,403);
  assert.equal(hits.length,0);
  assert.equal((await call(path,owner)).status,200);
  assert.equal((await call(path,owner,"POST",{Origin:"https://"+host,"Content-Type":"application/x-www-form-urlencoded"})).status,200);
  for(const r of hits){
    assert.equal(r.url,origin+internal);assert.equal(r.headers.get("X-Split-Browser-Review"),"1");
    for(const k of ["Cookie","CF-Access-Jwt-Assertion","Authorization","X-Split-Remote-Tool"])assert.equal(r.headers.get(k),null);
  }
  assert.equal((await call(internal,owner)).status,200); // Origin must still refuse requests without the marker.
  assert.equal(hits.at(-1).headers.get("X-Split-Browser-Review"),null);
  const r=await call("/api/prompt",service,"POST");
  assert.equal(r.status,409);assert.ok((await r.json()).error.message.includes("https://"+host+path));
  assert.equal(hits.at(-1).url,origin+"/_remote_mcp/submit");
  assert.equal(hits.at(-1).headers.get("X-Split-Browser-Review"),null);
  assert.equal(hits.at(-1).headers.get("X-Split-Remote-Tool"),"1");
  assert.equal((await call("/split/mode",owner,"POST")).status,200);
  assert.equal(hits.at(-1).headers.get("X-Split-Browser-Review"),null);
});
