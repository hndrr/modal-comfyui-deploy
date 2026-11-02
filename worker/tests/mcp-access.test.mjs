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
