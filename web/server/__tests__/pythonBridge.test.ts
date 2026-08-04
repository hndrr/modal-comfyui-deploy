import { afterEach, describe, expect, it, vi } from "vitest";
import { PythonAssetBridge, defaultTimeoutFor } from "../lib/pythonBridge.js";

const originalTimeout = process.env.COMFY_ASSET_RPC_TIMEOUT_MS;

afterEach(() => {
  if (originalTimeout === undefined) {
    delete process.env.COMFY_ASSET_RPC_TIMEOUT_MS;
  } else {
    process.env.COMFY_ASSET_RPC_TIMEOUT_MS = originalTimeout;
  }
});

describe("defaultTimeoutFor", () => {
  it("uses the global timeout as a minimum except for startup health", () => {
    process.env.COMFY_ASSET_RPC_TIMEOUT_MS = "240000";

    expect(defaultTimeoutFor("health")).toBe(15_000);
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

describe("PythonAssetBridge lifecycle", () => {
  type BridgeInternals = {
    child: { killed: boolean } | null;
    closing: boolean;
    closed: boolean;
    ensureStarted: () => Promise<void>;
    forceKillWorker: (error: Error, options: { restart: boolean }) => void;
  };

  it("does not restart a timed-out worker while closing", () => {
    const bridge = new PythonAssetBridge();
    const internals = bridge as unknown as BridgeInternals;
    internals.closing = true;
    const ensureStarted = vi
      .spyOn(internals, "ensureStarted")
      .mockResolvedValue(undefined);

    internals.forceKillWorker(new Error("timeout"), { restart: true });

    expect(ensureStarted).not.toHaveBeenCalled();
  });

  it("stays closed after shutdown fails", async () => {
    const bridge = new PythonAssetBridge();
    const internals = bridge as unknown as BridgeInternals;
    internals.child = { killed: true };
    const shutdown = vi
      .spyOn(bridge, "call")
      .mockRejectedValueOnce(new Error("shutdown timeout"));

    await bridge.close();
    shutdown.mockRestore();

    expect(internals.closed).toBe(true);
    expect(internals.closing).toBe(false);
    await expect(bridge.call("health")).rejects.toThrow(/closed/);
  });
});
