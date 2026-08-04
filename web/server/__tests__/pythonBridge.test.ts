import { afterEach, describe, expect, it } from "vitest";
import { defaultTimeoutFor } from "../lib/pythonBridge.js";

const originalTimeout = process.env.COMFY_ASSET_RPC_TIMEOUT_MS;

afterEach(() => {
  if (originalTimeout === undefined) {
    delete process.env.COMFY_ASSET_RPC_TIMEOUT_MS;
  } else {
    process.env.COMFY_ASSET_RPC_TIMEOUT_MS = originalTimeout;
  }
});

describe("defaultTimeoutFor", () => {
  it("uses the global timeout as a minimum for every method", () => {
    process.env.COMFY_ASSET_RPC_TIMEOUT_MS = "240000";

    expect(defaultTimeoutFor("health")).toBe(240_000);
    expect(defaultTimeoutFor("list")).toBe(240_000);
    expect(defaultTimeoutFor("upload")).toBe(600_000);
    expect(defaultTimeoutFor("unknown")).toBe(240_000);
  });

  it("ignores invalid and non-positive global timeouts", () => {
    process.env.COMFY_ASSET_RPC_TIMEOUT_MS = "invalid";
    expect(defaultTimeoutFor("health")).toBe(15_000);

    process.env.COMFY_ASSET_RPC_TIMEOUT_MS = "0";
    expect(defaultTimeoutFor("health")).toBe(15_000);
  });
});
