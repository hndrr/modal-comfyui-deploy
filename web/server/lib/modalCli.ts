import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ModalLsRow } from "./types.js";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

export type RunResult = {
  stdout: string;
  stderr: string;
  code: number;
};

export type ModalCliRunner = (
  args: string[],
  options?: { cwd?: string },
) => Promise<RunResult>;

let cachedModalInvocation: string[] | null = null;

/** Modal CLI may emit rich/ANSI markup even with --json. */
export function stripAnsi(text: string): string {
  return text
    .replace(/\u001b\[[0-9;]*m/g, "")
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, "");
}

function enrichedEnv(): NodeJS.ProcessEnv {
  const home = process.env.HOME ?? "";
  const extras = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    home ? `${home}/.local/bin` : "",
    home ? `${home}/.cargo/bin` : "",
  ].filter(Boolean);
  const pathValue = process.env.PATH ?? "";
  return {
    ...process.env,
    PATH: [...extras, pathValue].join(":"),
    // Prefer plain output when the CLI honors these.
    NO_COLOR: "1",
    FORCE_COLOR: "0",
    CLICOLOR: "0",
  };
}

async function detectModalInvocation(): Promise<string[]> {
  if (cachedModalInvocation) return cachedModalInvocation;

  const tryRun = async (command: string, args: string[]): Promise<boolean> => {
    try {
      const result = await runProcess(command, args, { timeoutMs: 60_000 });
      return result.code === 0;
    } catch {
      return false;
    }
  };

  // Prefer repo-local uv so the same Modal version as Python deps is used.
  if (await tryRun("uv", ["run", "modal", "--version"])) {
    cachedModalInvocation = ["uv", "run", "modal"];
  } else if (await tryRun("modal", ["--version"])) {
    cachedModalInvocation = ["modal"];
  } else {
    throw new Error(
      "Modal CLI not found. Install Modal and run `modal setup`, or ensure `uv run modal` works in the repo root.",
    );
  }
  return cachedModalInvocation;
}

function runProcess(
  command: string,
  args: string[],
  options: { cwd?: string; timeoutMs?: number } = {},
): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd ?? REPO_ROOT,
      env: enrichedEnv(),
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`Command timed out: ${command} ${args.join(" ")}`));
    }, options.timeoutMs ?? 120_000);

    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      resolve({ stdout, stderr, code: code ?? 1 });
    });
  });
}

export const defaultModalCliRunner: ModalCliRunner = async (args, options) => {
  const invocation = await detectModalInvocation();
  const [command, ...prefix] = invocation;
  return runProcess(command, [...prefix, ...args], {
    cwd: options?.cwd ?? REPO_ROOT,
    timeoutMs: 300_000,
  });
};

export async function modalVolumeLs(
  volume: string,
  remotePath: string,
  runner: ModalCliRunner = defaultModalCliRunner,
): Promise<ModalLsRow[]> {
  const pathArg = remotePath ? `/${remotePath.replace(/^\/+/, "")}` : "/";
  const result = await runner(["volume", "ls", volume, pathArg, "--json"]);
  if (result.code !== 0) {
    throw new Error(
      stripAnsi(result.stderr).trim() ||
        stripAnsi(result.stdout).trim() ||
        "modal volume ls failed",
    );
  }
  const text = stripAnsi(result.stdout).trim();
  if (!text) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new Error(
      `Failed to parse modal volume ls JSON: ${error instanceof Error ? error.message : error}`,
    );
  }
  if (!Array.isArray(parsed)) {
    throw new Error("Unexpected modal volume ls JSON shape");
  }
  return parsed as ModalLsRow[];
}

export async function modalVolumeGet(
  volume: string,
  remotePath: string,
  localDestination: string,
  runner: ModalCliRunner = defaultModalCliRunner,
): Promise<void> {
  await fs.mkdir(path.dirname(localDestination), { recursive: true });
  const remote = remotePath.startsWith("/") ? remotePath : `/${remotePath}`;
  const result = await runner(["volume", "get", volume, remote, localDestination, "--force"]);
  if (result.code !== 0) {
    throw new Error(
      stripAnsi(result.stderr).trim() ||
        stripAnsi(result.stdout).trim() ||
        "modal volume get failed",
    );
  }
}

export async function modalVolumePut(
  volume: string,
  localPath: string,
  remotePath: string,
  force: boolean,
  runner: ModalCliRunner = defaultModalCliRunner,
): Promise<void> {
  const remote = remotePath.startsWith("/") ? remotePath : `/${remotePath}`;
  const args = ["volume", "put", volume, localPath, remote];
  if (force) args.push("--force");
  const result = await runner(args);
  if (result.code !== 0) {
    throw new Error(
      stripAnsi(result.stderr).trim() ||
        stripAnsi(result.stdout).trim() ||
        "modal volume put failed",
    );
  }
}

export async function modalVolumeRm(
  volume: string,
  remotePath: string,
  recursive: boolean,
  runner: ModalCliRunner = defaultModalCliRunner,
): Promise<void> {
  const remote = remotePath.startsWith("/") ? remotePath : `/${remotePath}`;
  const args = ["volume", "rm", volume, remote];
  if (recursive) args.push("--recursive");
  const result = await runner(args);
  if (result.code !== 0) {
    throw new Error(
      stripAnsi(result.stderr).trim() ||
        stripAnsi(result.stdout).trim() ||
        "modal volume rm failed",
    );
  }
}

export async function modalVolumeCp(
  volume: string,
  sourcePath: string,
  destinationPath: string,
  recursive: boolean,
  runner: ModalCliRunner = defaultModalCliRunner,
): Promise<void> {
  const source = sourcePath.startsWith("/") ? sourcePath : `/${sourcePath}`;
  const destination = destinationPath.startsWith("/")
    ? destinationPath
    : `/${destinationPath}`;
  const args = ["volume", "cp", volume, source, destination];
  if (recursive) args.push("--recursive");
  const result = await runner(args);
  if (result.code !== 0) {
    throw new Error(
      stripAnsi(result.stderr).trim() ||
        stripAnsi(result.stdout).trim() ||
        "modal volume cp failed",
    );
  }
}

export async function withTempDir<T>(fn: (dir: string) => Promise<T>): Promise<T> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "comfy-assets-"));
  try {
    return await fn(dir);
  } finally {
    await fs.rm(dir, { recursive: true, force: true });
  }
}

export function resetModalInvocationCache(): void {
  cachedModalInvocation = null;
}
