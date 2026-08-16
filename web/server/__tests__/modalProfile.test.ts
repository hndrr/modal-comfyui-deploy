import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  REPO_PROFILE_FILE,
  modalProfileEnv,
  readRepoModalProfile,
  resolveModalProfile,
} from "../lib/modalProfile.js";

const directories: string[] = [];

afterEach(() => {
  for (const directory of directories.splice(0)) {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

function repoWith(contents: string | null): string {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "modal-profile-test-"));
  directories.push(directory);
  if (contents !== null) {
    fs.writeFileSync(path.join(directory, REPO_PROFILE_FILE), contents, "utf8");
  }
  return directory;
}

describe("readRepoModalProfile", () => {
  it("reads the first meaningful line and ignores comments", () => {
    expect(readRepoModalProfile(repoWith("# pinned account\nalpha\n"))).toBe("alpha");
    expect(readRepoModalProfile(repoWith("  alpha  \n"))).toBe("alpha");
  });

  it("treats a missing, empty or comment-only file as unpinned", () => {
    expect(readRepoModalProfile(repoWith(null))).toBeNull();
    expect(readRepoModalProfile(repoWith(""))).toBeNull();
    expect(readRepoModalProfile(repoWith("\n\n"))).toBeNull();
    expect(readRepoModalProfile(repoWith("# only a comment\n"))).toBeNull();
  });
});

describe("resolveModalProfile", () => {
  it("prefers MODAL_PROFILE over the repo pin", () => {
    const repoRoot = repoWith("alpha\n");

    expect(resolveModalProfile({ MODAL_PROFILE: "beta" }, repoRoot)).toEqual({
      profile: "beta",
      source: "env",
    });
    expect(modalProfileEnv({ MODAL_PROFILE: "beta" }, repoRoot)).toEqual({
      MODAL_PROFILE: "beta",
    });
  });

  it("falls back to the repo pin, then to the active profile", () => {
    const pinned = repoWith("alpha\n");
    const unpinned = repoWith(null);

    expect(resolveModalProfile({}, pinned)).toEqual({ profile: "alpha", source: "repo" });
    expect(modalProfileEnv({}, pinned)).toEqual({ MODAL_PROFILE: "alpha" });

    expect(resolveModalProfile({}, unpinned)).toEqual({ profile: null, source: "active" });
    // No override, so the spawned process uses whatever ~/.modal.toml says.
    expect(modalProfileEnv({}, unpinned)).toEqual({});
  });

  it("ignores a blank MODAL_PROFILE", () => {
    const repoRoot = repoWith("alpha\n");

    expect(resolveModalProfile({ MODAL_PROFILE: "   " }, repoRoot)).toEqual({
      profile: "alpha",
      source: "repo",
    });
  });
});
