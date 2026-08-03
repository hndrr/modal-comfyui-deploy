import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import readline from "node:readline";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

type Pending = {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  onProgress?: (value: unknown) => void;
};

export type BridgeCallOptions = {
  onProgress?: (value: unknown) => void;
};

/**
 * One warm Python process running asset_rpc.py (Modal SDK in-process).
 */
export class PythonAssetBridge {
  private child: ChildProcessWithoutNullStreams | null = null;
  private rl: readline.Interface | null = null;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private starting: Promise<void> | null = null;

  async call<T>(
    method: string,
    params: Record<string, unknown> = {},
    options: BridgeCallOptions = {},
  ): Promise<T> {
    await this.ensureStarted();
    const id = this.nextId++;
    const payload = JSON.stringify({ id, method, params });
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
        onProgress: options.onProgress,
      });
      if (!this.child?.stdin.writable) {
        this.pending.delete(id);
        reject(new Error("Python asset worker is not writable"));
        return;
      }
      this.child.stdin.write(`${payload}\n`, (error) => {
        if (error) {
          this.pending.delete(id);
          reject(error);
        }
      });
    });
  }

  async close(): Promise<void> {
    if (!this.child) return;
    try {
      await this.call("shutdown", {});
    } catch {
      // ignore
    }
    this.child.kill("SIGTERM");
    this.child = null;
    this.rl?.close();
    this.rl = null;
  }

  private ensureStarted(): Promise<void> {
    if (this.child && !this.child.killed) return Promise.resolve();
    if (this.starting) return this.starting;

    this.starting = new Promise<void>((resolve, reject) => {
      const env = {
        ...process.env,
        PATH: ["/opt/homebrew/bin", "/usr/local/bin", process.env.PATH ?? ""].join(":"),
        PYTHONUNBUFFERED: "1",
      };
      const child = spawn("uv", ["run", "python", "asset_rpc.py"], {
        cwd: REPO_ROOT,
        env,
        stdio: ["pipe", "pipe", "pipe"],
      });
      this.child = child;
      this.rl = readline.createInterface({ input: child.stdout });
      this.rl.on("line", (line) => this.onLine(line));
      child.stderr.on("data", (chunk: Buffer) => {
        const text = chunk.toString("utf8").trim();
        if (text) console.error("[asset_rpc]", text);
      });
      child.on("error", (error) => {
        this.failAll(error);
        this.child = null;
        this.starting = null;
        reject(error);
      });
      child.on("exit", (code, signal) => {
        this.failAll(
          new Error(`Python asset worker exited (code=${code}, signal=${signal})`),
        );
        this.child = null;
        this.rl?.close();
        this.rl = null;
        this.starting = null;
      });

      const id = this.nextId++;
      this.pending.set(id, {
        resolve: () => {
          this.starting = null;
          resolve();
        },
        reject: (error) => {
          this.starting = null;
          reject(error);
        },
      });
      child.stdin.write(
        `${JSON.stringify({ id, method: "health", params: {} })}\n`,
        (error) => {
          if (error) {
            this.pending.delete(id);
            this.starting = null;
            reject(error);
          }
        },
      );
    });

    return this.starting;
  }

  private onLine(line: string): void {
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

    // Streaming progress for long-running methods (e.g. delete_many).
    if (message.partial) {
      if (message.ok) pending.onProgress?.(message.result);
      return;
    }

    this.pending.delete(id);
    if (message.ok) {
      pending.resolve(message.result);
    } else {
      pending.reject(new Error(message.error || "Python asset worker error"));
    }
  }

  private failAll(error: Error): void {
    for (const pending of this.pending.values()) {
      pending.reject(error);
    }
    this.pending.clear();
  }
}

export const defaultPythonBridge = new PythonAssetBridge();
