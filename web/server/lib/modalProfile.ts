import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

/** Repo-local pin: which Modal account this checkout works against. */
export const REPO_PROFILE_FILE = ".modal-profile";

/** Where the profile came from, so the UI can explain what it is showing. */
export type ModalProfileSource = "env" | "repo" | "active";

export type ResolvedModalProfile = {
  /** Null means "whatever ~/.modal.toml has marked active". */
  profile: string | null;
  source: ModalProfileSource;
};

/**
 * Read the profile pinned for this checkout. Blank or missing file means the
 * repo is not pinned; the global active profile is used instead.
 */
export function readRepoModalProfile(repoRoot: string = REPO_ROOT): string | null {
  try {
    const raw = fs.readFileSync(path.join(repoRoot, REPO_PROFILE_FILE), "utf8");
    // Allow a trailing comment line so the file can explain itself.
    const line = raw
      .split("\n")
      .map((entry) => entry.trim())
      .find((entry) => entry && !entry.startsWith("#"));
    return line || null;
  } catch {
    return null;
  }
}

/** MODAL_PROFILE in the environment wins over the repo pin, which wins over the active profile. */
export function resolveModalProfile(
  env: NodeJS.ProcessEnv = process.env,
  repoRoot: string = REPO_ROOT,
): ResolvedModalProfile {
  const fromEnv = env.MODAL_PROFILE?.trim();
  if (fromEnv) return { profile: fromEnv, source: "env" };

  const fromRepo = readRepoModalProfile(repoRoot);
  if (fromRepo) return { profile: fromRepo, source: "repo" };

  return { profile: null, source: "active" };
}

/**
 * Environment overlay for spawned Modal processes (`modal` CLI and asset_rpc.py),
 * so both hit the account this checkout is pinned to.
 */
export function modalProfileEnv(
  env: NodeJS.ProcessEnv = process.env,
  repoRoot: string = REPO_ROOT,
): { MODAL_PROFILE?: string } {
  const resolved = resolveModalProfile(env, repoRoot);
  return resolved.profile ? { MODAL_PROFILE: resolved.profile } : {};
}
