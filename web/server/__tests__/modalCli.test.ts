import { describe, expect, it, vi } from "vitest";
import { modalActiveProfile } from "../lib/modalCli.js";
import type { ModalCliRunner, RunResult } from "../lib/modalCli.js";

function runner(result: Partial<RunResult>): ModalCliRunner {
  return vi.fn(async () => ({ stdout: "", stderr: "", code: 0, ...result }));
}

describe("modalActiveProfile", () => {
  it("returns the active profile and its workspace", async () => {
    const call = runner({
      stdout: JSON.stringify([
        { name: "alpha", workspace: "alpha-workspace", active: false },
        { name: "beta", workspace: "beta-workspace", active: true },
      ]),
    });

    await expect(modalActiveProfile(call, "repo")).resolves.toEqual({
      profile: "beta",
      workspace: "beta-workspace",
      source: "repo",
    });
    expect(call).toHaveBeenCalledWith(["profile", "list", "--json"]);
  });

  it("strips ANSI markup emitted by the rich console", async () => {
    const call = runner({
      stdout: `\u001b[1m${JSON.stringify([
        { name: "alpha", workspace: "alpha-workspace", active: true },
      ])}\u001b[0m\n`,
    });

    await expect(modalActiveProfile(call, "env")).resolves.toEqual({
      profile: "alpha",
      workspace: "alpha-workspace",
      source: "env",
    });
  });

  it("drops the workspace when the lookup failed but keeps the profile", async () => {
    const call = runner({
      stdout: JSON.stringify([
        { name: "alpha", workspace: "Unknown (authentication failure)", active: true },
      ]),
    });

    await expect(modalActiveProfile(call, "active")).resolves.toEqual({
      profile: "alpha",
      workspace: null,
      source: "active",
    });
  });

  it("returns null when MODAL_PROFILE names an unconfigured profile", async () => {
    const call = runner({
      stdout: JSON.stringify([
        { name: "alpha", workspace: "alpha-workspace", active: false },
      ]),
    });

    await expect(modalActiveProfile(call)).resolves.toBeNull();
  });

  it("returns null on a failed or unparsable CLI call", async () => {
    await expect(
      modalActiveProfile(runner({ code: 1, stderr: "not logged in" })),
    ).resolves.toBeNull();
    await expect(modalActiveProfile(runner({ stdout: "not json" }))).resolves.toBeNull();
    await expect(modalActiveProfile(runner({ stdout: "{}" }))).resolves.toBeNull();
    await expect(modalActiveProfile(runner({ stdout: "" }))).resolves.toBeNull();
  });
});
