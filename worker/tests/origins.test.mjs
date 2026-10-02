import assert from "node:assert/strict";
import { test } from "node:test";
import worker, { resolveUpstreamOrigin, buildUpstreamHeaders } from "../src/index.ts";

const env = {
  MODAL_ORIGINS: JSON.stringify({ "comfy.example.com": "https://production.modal.run" }),
  TEAM_DOMAIN: "https://example.cloudflareaccess.com",
  POLICY_AUD: "test", MODAL_KEY: "test", MODAL_SECRET: "test",
};

test("extra hosts preserve production and unknown hosts stay closed", () => {
  const extra = { ...env, MODAL_ADDITIONAL_ORIGINS: JSON.stringify({
    "comfy-staging.example.com": "https://staging.modal.run",
  }) };
  assert.equal(resolveUpstreamOrigin(env, "comfy.example.com"), "https://production.modal.run");
  assert.equal(resolveUpstreamOrigin(extra, "comfy.example.com"), "https://production.modal.run");
  assert.equal(resolveUpstreamOrigin(extra, "comfy-staging.example.com"), "https://staging.modal.run");
  assert.equal(resolveUpstreamOrigin(extra, "other.example.com"), null);
});

test("extra hosts reject production overrides and insecure origins", () => {
  for (const extra of [
    { "comfy.example.com": "https://staging.modal.run" },
    { "staging.example.com": "http://staging.modal.run" },
  ]) {
    assert.throws(() => resolveUpstreamOrigin({
      ...env, MODAL_ADDITIONAL_ORIGINS: JSON.stringify(extra),
    }, "comfy.example.com"));
  }
});

test("staging requests still require an Access JWT", async () => {
  const response = await worker.fetch(new Request("https://staging.example.com/"), {
    ...env, MODAL_ADDITIONAL_ORIGINS: '{"staging.example.com":"https://staging.modal.run"}',
  });
  assert.equal(response.status, 403);
});

test("credentials follow the selected Modal origin without changing other origins", () => {
  const config = { ...env, MODAL_PROXY_CREDENTIALS: JSON.stringify({
    "https://staging.modal.run": { key: "wk-staging", secret: "ws-staging" },
  }) };
  const request = new Request("https://staging.example.com/ws", { headers: {
    "Modal-Key": "forged", "Modal-Secret": "forged",
    "Cf-Access-Jwt-Assertion": "private", "Cookie": "CF_Authorization=private; other=kept",
  } });
  const staging = buildUpstreamHeaders(request, config, "https://staging.modal.run");
  assert.equal(staging.get("Modal-Key"), "wk-staging");
  assert.equal(staging.get("Modal-Secret"), "ws-staging");
  assert.equal(staging.get("Cf-Access-Jwt-Assertion"), null);
  assert.equal(staging.get("Cookie"), "other=kept");
  const production = buildUpstreamHeaders(request, config, "https://production.modal.run");
  assert.equal(production.get("Modal-Key"), env.MODAL_KEY);
  assert.equal(production.get("Modal-Secret"), env.MODAL_SECRET);
  assert.throws(() => buildUpstreamHeaders(request, {
    ...env, MODAL_PROXY_CREDENTIALS: '{"https://staging.modal.run":{"key":"missing-secret"}}',
  }, "https://staging.modal.run"));
});
