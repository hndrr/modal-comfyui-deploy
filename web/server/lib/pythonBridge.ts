import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import readline from "node:readline";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

/** Per-method defaults (ms). Override any call with `options.timeoutMs`. */
const DEFAULT_TIMEOUT_MS: Record<string, number> = {
  health: 15_000,
  list: 120_000,
  materialize: 300_000,
  upload: 600_000,
  move: 600_000,
  mkdir: 180_000,
  delete: 180_000,
  delete_many: 900_000,
  shutdown: 5_000,
};

const FALLBACK_TIMEOUT_MS = 180_000;
const SHUTDOWN_FORCE_MS = 3_000;

type Pending = {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  onProgress?: (value: unknown) => void;
  timer: ReturnType<typeof setTimeout>;
  timeoutMs: number;
  method: string;
  /** Generation of the worker that accepted this call. */
  generation: number;
};

export type BridgeCallOptions = {
  onProgress?: (value: unknown) => void;
  /** Hard deadline for this RPC. Defaults depend on method. */
  timeoutMs?: number;
};

export function defaultTimeoutFor(method: string): number {
  const methodDefault = DEFAULT_TIMEOUT_MS[method] ?? FALLBACK_TIMEOUT_MS;
  const envDefault = process.env.COMFY_ASSET_RPC_TIMEOUT_MS;
  if (envDefault && Number.isFinite(Number(envDefault))) {
    const n = Number(envDefault);
    if (n > 0) return Math.max(methodDefault, n);
  }
  return methodDefault;
}

/**
 * One warm Python process running asset_rpc.py (Modal SDK in-process).
 * Every call has a deadline; hung workers are killed and restarted.
 */
export class PythonAssetBridge {
  private child: ChildProcessWithoutNullStreams | null = null;
  private rl: readline.Interface | null = null;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private starting: Promise<void> | null = null;
  /** Bumps on each spawn so late replies from a killed process are ignored. */
  private generation = 0;
  private restarting = false;

  async call<T>(
    method: string,
    params: Record<string, unknown> = {},
    options: BridgeCallOptions = {},
  ): Promise<T> {
    await this.ensureStarted();
    const id = this.nextId++;
    const timeoutMs = Math.max(
      1_000,
      options.timeoutMs ?? defaultTimeoutFor(method),
    );
    const generation = this.generation;
    const payload = JSON.stringify({ id, method, params });

    return new Promise<T>((resolve, reject) => {
      const armTimer = (): ReturnType<typeof setTimeout> =>
        setTimeout(() => {
          void this.onCallTimeout(id, method, timeoutMs);
        }, timeoutMs);

      this.pending.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
        onProgress: options.onProgress,
        timer: armTimer(),
        timeoutMs,
        method,
        generation,
      });

      if (!this.child?.stdin.writable) {
        this.finishPending(id, { error: new Error("Python asset worker is not writable") });
        return;
      }
      this.child.stdin.write(`${payload}\n`, (error) => {
        if (error) {
          this.finishPending(id, { error });
        }
      });
    });
  }

  async close(): Promise<void> {
    if (!this.child) return;
    try {
      await this.call("shutdown", {}, { timeoutMs: DEFAULT_TIMEOUT_MS.shutdown });
    } catch {
      // ignore — force kill below
    }
    this.forceKillWorker(
      new Error("Python asset worker closed"),
      { restart: false },
    );
  }

  private finishPending(
    id: number,
    outcome: { result?: unknown; error?: Error },
  ): void {
    const pending = this.pending.get(id);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending.delete(id);
    if (outcome.error) pending.reject(outcome.error);
    else pending.resolve(outcome.result);
  }

  private async onCallTimeout(
    id: number,
    method: string,
    timeoutMs: number,
  ): Promise<void> {
    const pending = this.pending.get(id);
    if (!pending) return;

    const error = new Error(
      `Python asset worker timed out after ${timeoutMs}ms (method=${method})`,
    );
    // Reject this call first, then recycle the worker so a stuck Modal op
    // cannot block every subsequent HTTP request forever.
    this.finishPending(id, { error });
    console.error(`[asset_rpc] ${error.message}; restarting worker`);
    this.forceKillWorker(error, { restart: true });
  }

  private forceKillWorker(
    error: Error,
    options: { restart: boolean },
  ): void {
    if (this.restarting) {
      this.failAll(error);
      return;
    }
    this.restarting = true;
    try {
      // Invalidate in-flight line handlers / exit handlers from this process
      // before a replacement is spawned.
      this.generation += 1;
      const child = this.child;
      this.child = null;
      this.rl?.close();
      this.rl = null;
      this.starting = null;
      this.failAll(error);
      if (child && !child.killed) {
        try {
          child.kill("SIGTERM");
        } catch {
          // ignore
        }
        // Escalate if the process ignores SIGTERM (e.g. stuck in native code).
        const forceTimer = setTimeout(() => {
          try {
            if (!child.killed) child.kill("SIGKILL");
          } catch {
            // ignore
          }
        }, SHUTDOWN_FORCE_MS);
        forceTimer.unref?.();
        child.once("exit", () => clearTimeout(forceTimer));
      }
    } finally {
      this.restarting = false;
    }
    // Next call() will spawn a fresh worker via ensureStarted().
    if (options.restart) {
      // Do not await here — callers already got their reject.
      void this.ensureStarted().catch((err) => {
        console.error(
          "[asset_rpc] failed to restart worker:",
          err instanceof Error ? err.message : err,
        );
      });
    }
  }

  private ensureStarted(): Promise<void> {
    if (this.child && !this.child.killed) return Promise.resolve();
    if (this.starting) return this.starting;

    this.starting = new Promise<void>((resolve, reject) => {
      this.generation += 1;
      const generation = this.generation;
      const env = {
        ...process.env,
        PATH: ["/opt/homebrew/bin", "/usr/local/bin", process.env.PATH ?? ""].join(
          ":",
        ),
        PYTHONUNBUFFERED: "1",
      };
      const child = spawn("uv", ["run", "python", "asset_rpc.py"], {
        cwd: REPO_ROOT,
        env,
        stdio: ["pipe", "pipe", "pipe"],
      });
      this.child = child;
      this.rl = readline.createInterface({ input: child.stdout });
      this.rl.on("line", (line) => this.onLine(line, generation));
      child.stderr.on("data", (chunk: Buffer) => {
        const text = chunk.toString("utf8").trim();
        if (text) console.error("[asset_rpc]", text);
      });
      child.on("error", (error) => {
        if (this.generation !== generation) return;
        this.failAll(error);
        this.child = null;
        this.starting = null;
        reject(error);
      });
      child.on("exit", (code, signal) => {
        if (this.generation !== generation) return;
        this.failAll(
          new Error(
            `Python asset worker exited (code=${code}, signal=${signal})`,
          ),
        );
        this.child = null;
        this.rl?.close();
        this.rl = null;
        this.starting = null;
      });

      const id = this.nextId++;
      const timeoutMs = defaultTimeoutFor("health");
      const timer = setTimeout(() => {
        this.finishPending(id, {
          error: new Error(
            `Python asset worker health check timed out after ${timeoutMs}ms`,
          ),
        });
        this.forceKillWorker(
          new Error("Python asset worker failed health check"),
          { restart: false },
        );
        reject(new Error("Python asset worker health check timed out"));
      }, timeoutMs);

      this.pending.set(id, {
        resolve: () => {
          this.starting = null;
          resolve();
        },
        reject: (error) => {
          this.starting = null;
          reject(error);
        },
        timer,
        timeoutMs,
        method: "health",
        generation,
      });
      child.stdin.write(
        `${JSON.stringify({ id, method: "health", params: {} })}\n`,
        (error) => {
          if (error) {
            this.finishPending(id, { error });
            this.starting = null;
            reject(error);
          }
        },
      );
    });

    return this.starting;
  }

  private onLine(line: string, generation: number): void {
    if (generation !== this.generation) return;

    let message: {
      id?: number;
      ok?: boolean;
      partial?: boolean;
      result?: unknown;
      error?: string;
    };
    try {
      message = JSON.parse(line);
    } catch {
      console.error("[asset_rpc] bad JSON line", line.slice(0, 200));
      return;
    }
    const id = message.id;
    if (id == null) return;
    const pending = this.pending.get(id);
    if (!pending) return;
    if (pending.generation !== generation) return;

    // Streaming progress for long-running methods (e.g. delete_many).
    // Refresh the deadline so steady progress is not killed mid-batch.
    if (message.partial) {
      if (message.ok) {
        clearTimeout(pending.timer);
        pending.timer = setTimeout(() => {
          void this.onCallTimeout(id, pending.method, pending.timeoutMs);
        }, pending.timeoutMs);
        pending.onProgress?.(message.result);
      }
      return;
    }

    clearTimeout(pending.timer);
    this.pending.delete(id);
    if (message.ok) {
      pending.resolve(message.result);
    } else {
      pending.reject(new Error(message.error || "Python asset worker error"));
    }
  }

  private failAll(error: Error): void {
    const entries = [...this.pending.values()];
    this.pending.clear();
    for (const pending of entries) {
      clearTimeout(pending.timer);
      try {
        pending.reject(error);
      } catch {
        // ignore double-reject
      }
    }
  }
}

export const defaultPythonBridge = new PythonAssetBridge();
