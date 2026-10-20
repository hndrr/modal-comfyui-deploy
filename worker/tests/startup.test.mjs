import assert from "node:assert/strict";
import { test } from "node:test";
import { generateKeyPair, exportJWK, SignJWT } from "jose";
import worker from "../src/index.ts";
import { startupOrigins, startupResponse } from "../src/startup.ts";

test("startup handling is explicit and only accepts configured origins", () => {
  assert.equal(startupOrigins(undefined, ["https://test.modal.run"]).size, 0);
  assert.deepEqual([...startupOrigins('["https://test.modal.run"]', ["https://test.modal.run"])], ["https://test.modal.run"]);
  for (const raw of ['{}', '["https://unknown.modal.run"]', '[2]', '["http://test.modal.run"]']) {
    assert.throws(() => startupOrigins(raw, ["https://test.modal.run"]));
  }
});

test("only HTML GETs replace a 503, without replaying unsafe requests", async () => {
  const navigation = new Request("https://test.example.com/", { headers: { Accept: "text/html" } });
  const loading = startupResponse(navigation, new Response("not ready", { status: 503 }), true);
  assert.equal(loading.status, 503);
  assert.equal(loading.headers.get("Cache-Control"), "no-store");
  assert.match(await loading.text(), /fetch\('\/split\/startup'/);
  for (const request of [new Request(navigation, { method: "POST", body: "never replay" }),
    new Request(navigation, { headers: { Accept: "application/json" } }),
    new Request(navigation, { headers: { Accept: "text/html", Upgrade: "websocket" } })]) {
    const original = new Response("unchanged", { status: 503 });
    assert.equal(startupResponse(request, original, true), original);
  }
  for (const status of [200, 401, 403, 429, 500]) {
    const original = new Response("unchanged", { status });
    assert.equal(startupResponse(navigation, original, true), original);
  }
  const disabled = new Response("original", { status: 503 });
  assert.equal(startupResponse(navigation, disabled, false), disabled);
});

test("Access authentication precedes startup handling and each POST is forwarded once", async () => {
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  const jwk = { ...await exportJWK(publicKey), kid: "startup-test" };
  const env = { MODAL_ORIGINS: '{"test.example.com":"https://test.modal.run"}',
    MODAL_STARTUP_ORIGINS: '["https://test.modal.run"]',
    TEAM_DOMAIN: "https://startup-test.cloudflareaccess.com", POLICY_AUD: "test",
    MODAL_KEY: "wk-test", MODAL_SECRET: "ws-test" };
  const token = await new SignJWT({}).setProtectedHeader({ alg: "RS256", kid: jwk.kid })
    .setIssuer(env.TEAM_DOMAIN).setAudience("test").setExpirationTime("1h").sign(privateKey);
  const originalFetch = globalThis.fetch;
  const OriginalRequest = globalThis.Request;
  // Workers accepts streaming bodies directly; Node's implementation requires duplex.
  globalThis.Request = class extends OriginalRequest {
    constructor(input, init) { super(input, { ...init, ...(init?.body ? { duplex: "half" } : {}) }); }
  };
  const requests = [];
  globalThis.fetch = async (request) => {
    if (typeof request === "string" && request.endsWith("/cdn-cgi/access/certs")) {
      return Response.json({ keys: [jwk] });
    }
    requests.push(request);
    return new Response("upstream unavailable", { status: 503 });
  };
  try {
    const anonymous = await worker.fetch(new Request("https://test.example.com/", { headers: { Accept: "text/html" } }), env);
    assert.equal(anonymous.status, 403);
    assert.equal(requests.length, 0);
    const headers = { Accept: "text/html", "Cf-Access-Jwt-Assertion": token };
    const navigation = await worker.fetch(new Request("https://test.example.com/", { headers }), env);
    assert.match(await navigation.text(), /ComfyUIを起動/);
    assert.equal(requests.length, 1);
    const post = await worker.fetch(new Request("https://test.example.com/prompt", { method: "POST", headers, body: "job" }), env);
    assert.equal(await post.text(), "upstream unavailable");
    assert.equal(requests.length, 2);
    assert.equal(await requests[1].text(), "job");
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.Request = OriginalRequest;
  }
});
