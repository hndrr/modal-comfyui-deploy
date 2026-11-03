import assert from "node:assert/strict";
import { test } from "node:test";
import { exportJWK, generateKeyPair, SignJWT } from "jose";
import worker from "../src/index.ts";

test("owner-only pages share paths with existing ComfyUI JSON and bundles", async t => {
  const host = "split.example.com", team = "https://gui-test.cloudflareaccess.com";
  const ui = "https://split.modal.run", models = "https://model-gui.modal.run", assets = "https://asset-gui.modal.run";
  const env = { MODAL_ORIGINS:JSON.stringify({"other.example.com":"https://other.modal.run"}), SPLIT_UI_ORIGIN:JSON.stringify({[host]:ui}),
    TEAM_DOMAIN:team, POLICY_AUD:"ui", MODAL_KEY:"legacy", MODAL_SECRET:"legacy-secret",
    SPLIT_MCP_ACCESS:JSON.stringify({hostname:host, origin:ui, client_id:"machine.access"}),
    SPLIT_ADMIN_GUIS:JSON.stringify({hostname:host, owner_email:"owner@example.com",
      models_origin:models, assets_origin:assets, key:"gui-key", secret:"gui-secret"}) };
  const keys = await generateKeyPair("RS256");
  const jwk = {...await exportJWK(keys.publicKey),kid:"gui"};
  const sign = (claims, aud="ui") => new SignJWT({type:"app",...claims})
    .setProtectedHeader({alg:"RS256",kid:jwk.kid}).setIssuer(team).setAudience(aud).setExpirationTime("5m").sign(keys.privateKey);
  const owner = await sign({sub:"human",email:"owner@example.com"});
  const other = await sign({sub:"human",email:"other@example.com"});
  const machine = await sign({sub:"",common_name:"machine.access"});
  const oauth = await sign({sub:"human",email:"owner@example.com"},"mcp");
  const calls = [];
  t.mock.method(globalThis,"fetch",async req => {
    if (req === team + "/cdn-cgi/access/certs") return Response.json({keys:[jwk]});
    calls.push(req);
    return new Response("upstream",{headers:{"Set-Cookie":"internal=1"}});
  });
  const call = (path,token,accept="text/html",opts={}) => worker.fetch(new Request("https://"+host+path,{
    method:opts.method ?? "GET",headers:{Accept:accept, ...(token?{"CF-Access-Jwt-Assertion":token}:{}),
      Authorization:"Bearer other", Cookie:"CF_Binding=hidden", "CF-Access-Client-Secret":"hidden",
      "X-Forwarded-Host":"forged.example.com", ...opts.headers}}),env);
  for (const path of ["/models","/assets","/models/_ui/config","/assets/_ui/api/assets"]) {
    for (const token of [null,other,machine,oauth]) assert.equal((await call(path,token)).status,token===machine?410:403);
  }
  assert.equal((await call("/assets/_ui/api/assets",owner,"application/json",{method:"DELETE",headers:{Origin:"https://elsewhere.example"}})).status,403);
  assert.equal(calls.length,0);
  assert.match(await (await call("/models",owner)).text(),/src="\/models\/_ui\/"/);
  for (const [path, dst] of [["/assets",assets+"/"],["/models/_ui/config",models+"/config"],
      ["/assets/_ui/api/assets?volume=ok",assets+"/api/assets?volume=ok"]]) {
    const res = await call(path,owner);
    assert.equal(res.status,200);
    assert.equal(res.headers.get("Set-Cookie"),null);
    const req = calls.at(-1);
    assert.equal(req.url,dst);
    assert.equal(req.redirect,"manual");
    assert.equal(req.headers.get("Modal-Key"),"gui-key");
    for (const h of ["Authorization","Cookie","CF-Access-Client-Secret","CF-Access-Jwt-Assertion","X-Forwarded-Host"])
      assert.equal(req.headers.get(h),null);
  }
  for (const [path,accept] of [["/models","*/*"],["/models","application/json"],["/models/checkpoints","text/html"],
      ["/assets/index-a.js","*/*"],["/api/assets","application/json"]]) {
    assert.equal((await call(path,owner,accept)).status,200);
    assert.equal(calls.at(-1).url,ui+path);
  }
});

test("asset content safeguards follow the decoded route; malformed paths never reach Modal", async t => {
  const {splitGui} = await import("../src/split-guis.ts");
  const host = "split.example.com", origin = "https://asset-gui.modal.run";
  const email = "owner@example.com", cfg = JSON.stringify({hostname:host,owner_email:email,
    models_origin:"https://model-gui.modal.run",assets_origin:origin,key:"test",secret:"test"});
  const seen = [];
  t.mock.method(globalThis,"fetch",async req => {
    seen.push(req.url);
    return new Response("<script></script>", {headers:{"Content-Type":"text/html"}});
  });
  const get = suffix => splitGui(new Request("https://"+host+"/assets/_ui"+suffix),cfg,true,email);
  for (const path of ["/api/assets/content", "/api/assets/%63ontent", "/%61pi/%61ssets/con%74ent"]) {
    const r = await get(path+"?name=file");
    assert.equal(r.status,200);
    assert.equal(seen.at(-1),origin+path+"?name=file");
    assert.equal(r.headers.get("Content-Security-Policy"),"sandbox; default-src 'none'");
    assert.equal(r.headers.get("Content-Type"),"application/octet-stream");
    assert.equal(r.headers.get("X-Content-Type-Options"),"nosniff");
  }
  const count = seen.length;
  for (const path of ["/api/assets/%", "/api/assets/%FF"]) {
    assert.equal((await get(path)).status,400);
    assert.equal(seen.length,count);
  }
  assert.equal((await get("/api/assets/Content")).headers.get("Content-Security-Policy"),null);
});
