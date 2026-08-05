import { createLocalJWKSet, jwtVerify, type JSONWebKeySet } from "jose";

/**
 * Cloudflare Access で認証済みのリクエストだけを、Modal 上の ComfyUI へ中継する
 * リバースプロキシ。
 *
 * 責務は 4 つだけで、ComfyUI のパスや API は一切解釈しない。
 *   1. Access の JWT を検証する（不在・不正なら 403 = fail-closed）
 *   2. Modal-Key / Modal-Secret を付与する
 *   3. WebSocket（ComfyUI の /ws）を透過する
 *   4. Modal のホスト名を指す Location ヘッダーを書き換える
 *
 * 設計の背景は docs/cloudflare-access.md を参照。
 */

export interface Env {
  /** 例: https://<workspace>--comfyui-ui.modal.run */
  MODAL_ORIGIN: string;
  /** 例: https://<team-name>.cloudflareaccess.com */
  TEAM_DOMAIN: string;
  /** Access アプリの Application Audience (AUD) Tag */
  POLICY_AUD: string;
  /** Modal Proxy Auth Token の ID（wk- 始まり） */
  MODAL_KEY: string;
  /** Modal Proxy Auth Token の Secret（ws- 始まり） */
  MODAL_SECRET: string;
}

const ACCESS_JWT_HEADER = "Cf-Access-Jwt-Assertion";
const ACCESS_JWT_COOKIE = "CF_Authorization";

const REQUIRED_CONFIG_KEYS = [
  "MODAL_ORIGIN",
  "TEAM_DOMAIN",
  "POLICY_AUD",
  "MODAL_KEY",
  "MODAL_SECRET",
] as const satisfies readonly (keyof Env)[];

const JWKS_TTL_MS = 10 * 60 * 1000;

/**
 * キャッシュするのは JWKS の **JSON（プレーンなオブジェクト）だけ**。
 *
 * `createRemoteJWKSet` の戻り値をモジュールスコープに置いてはいけない。Workers は
 * あるリクエストのコンテキストで生成された I/O オブジェクトを別のリクエストから
 * 使うことを禁じており、2 回目以降の検証が必ず例外になる。
 * 素の JSON なら I/O を持たないので、リクエストをまたいで再利用してよい。
 */
let cachedJwks: { teamDomain: string; fetchedAt: number; keys: JSONWebKeySet } | undefined;

async function getKeySet(teamDomain: string) {
  const now = Date.now();
  const stale =
    !cachedJwks ||
    cachedJwks.teamDomain !== teamDomain ||
    now - cachedJwks.fetchedAt > JWKS_TTL_MS;

  if (stale) {
    const url = `${teamDomain}/cdn-cgi/access/certs`;
    const res = await fetch(url, { cf: { cacheTtl: 600, cacheEverything: true } });
    if (!res.ok) {
      throw new Error(`JWKS fetch failed: ${res.status} ${url}`);
    }
    cachedJwks = {
      teamDomain,
      fetchedAt: now,
      keys: await res.json<JSONWebKeySet>(),
    };
  }

  // 検証キーはリクエストごとに組み立てる（使い回さない）。
  return createLocalJWKSet(cachedJwks!.keys);
}

function normalizeTeamDomain(raw: string): string {
  return raw.trim().replace(/\/+$/, "");
}

/**
 * 転送前に設定を検証する。問題があれば理由を返し、無ければ null を返す。
 *
 * MODAL_ORIGIN が https でない場合も設定エラーとして扱う。http のオリジンへ
 * 転送すると Modal-Key / Modal-Secret が平文で流れるため、転送そのものを止める。
 */
function findConfigError(env: Env): string | null {
  const missing = REQUIRED_CONFIG_KEYS.filter((key) => !env[key]?.trim());
  if (missing.length > 0) {
    return `Missing configuration: ${missing.join(", ")}`;
  }

  let origin: URL;
  try {
    origin = new URL(env.MODAL_ORIGIN.trim());
  } catch {
    return `MODAL_ORIGIN is not a valid URL`;
  }
  if (origin.protocol !== "https:") {
    return `MODAL_ORIGIN must use https (got ${origin.protocol})`;
  }

  return null;
}

function readCookie(cookieHeader: string | null, name: string): string | null {
  if (!cookieHeader) {
    return null;
  }
  for (const pair of cookieHeader.split(";")) {
    const separator = pair.indexOf("=");
    if (separator === -1) {
      continue;
    }
    if (pair.slice(0, separator).trim() === name) {
      return pair.slice(separator + 1).trim();
    }
  }
  return null;
}

/**
 * Access が発行した JWT を取り出す。
 *
 * ヘッダーは Access がすべてのリクエストに付与する。クッキーはブラウザからの
 * リクエストにのみ含まれるため、フォールバックとして参照する。
 */
function readAccessToken(request: Request): string | null {
  return (
    request.headers.get(ACCESS_JWT_HEADER) ??
    readCookie(request.headers.get("Cookie"), ACCESS_JWT_COOKIE)
  );
}

/**
 * Access の JWT を検証する。
 *
 * この Worker は「すでに公開されているオリジン」の手前に立つため、Access の
 * 迂回を防ぐ責任はオリジン側、つまりこの Worker にある。workers_dev の設定漏れや
 * ルート誤設定があっても、ここで閉じられる。
 *
 * 許可メールアドレスの判定は Access のポリシー側に一本化しているので、
 * ここでは検証が通ったかどうかだけを見る。
 */
async function isAuthenticated(request: Request, env: Env): Promise<boolean> {
  const token = readAccessToken(request);
  if (!token) {
    console.warn("Access token not found on request (neither header nor cookie)");
    return false;
  }

  const teamDomain = normalizeTeamDomain(env.TEAM_DOMAIN);
  try {
    await jwtVerify(token, await getKeySet(teamDomain), {
      issuer: teamDomain,
      audience: env.POLICY_AUD.trim(),
    });
    return true;
  } catch (error) {
    // 失敗理由を残す。握り潰すと 403 の原因が追えなくなる。
    // トークン本体は出さない。
    console.warn(
      `Access token rejected: ${error instanceof Error ? `${error.name}: ${error.message}` : String(error)}`
    );
    return false;
  }
}

function upstreamUrlFor(requestUrl: URL, modalOrigin: string): URL {
  const upstreamUrl = new URL(modalOrigin);
  upstreamUrl.pathname = requestUrl.pathname;
  upstreamUrl.search = requestUrl.search;
  return upstreamUrl;
}

/**
 * 転送用のヘッダーを組み立てる。
 *
 * Host は Modal がルーティングに使うため、受け取った値を持ち越さず runtime に
 * 決めさせる。Access の JWT はここから先で使わないので落とす。
 */
function buildUpstreamHeaders(request: Request, env: Env): Headers {
  const headers = new Headers(request.headers);
  headers.delete("Host");
  headers.delete(ACCESS_JWT_HEADER);

  // Access の JWT はクッキーにも入っている。ヘッダーだけ落としても
  // Cookie 経由で ComfyUI へ渡ってしまうため、そちらも取り除く。
  // 他のクッキーは ComfyUI が使う可能性があるので残す。
  const cookie = stripAccessCookie(request.headers.get("Cookie"));
  if (cookie) {
    headers.set("Cookie", cookie);
  } else {
    headers.delete("Cookie");
  }

  // クライアントが偽装したヘッダーがあっても set で上書きされる。
  headers.set("Modal-Key", env.MODAL_KEY);
  headers.set("Modal-Secret", env.MODAL_SECRET);
  return headers;
}

/** Cookie ヘッダーから CF_Authorization だけを取り除いて組み立て直す。 */
function stripAccessCookie(cookieHeader: string | null): string | null {
  if (!cookieHeader) {
    return null;
  }
  const kept = cookieHeader
    .split(";")
    .filter((pair) => {
      const separator = pair.indexOf("=");
      const name = (separator === -1 ? pair : pair.slice(0, separator)).trim();
      return name !== ACCESS_JWT_COOKIE;
    })
    .map((pair) => pair.trim())
    .filter((pair) => pair.length > 0);

  return kept.length > 0 ? kept.join("; ") : null;
}

function isWebSocketUpgrade(request: Request): boolean {
  return request.headers.get("Upgrade")?.toLowerCase() === "websocket";
}

/**
 * ComfyUI の /ws を素通しする。
 *
 * **upstream のレスポンスをそのまま返すこと。** ここで
 * `new Response(null, { status: 101, webSocket: upstream.webSocket })` と
 * ソケットを取り出して包み直すと、その瞬間から Worker がソケットの保持者になる。
 * stateless Worker はレスポンスを返した時点で実行コンテキストが終わるため、
 * 保持したソケットは破棄され `Error: Network connection lost.` を出して
 * 0.5 秒ほどで切断される。`ctx.waitUntil()` でも延命できない。
 *
 * upstream の Response を素通しすれば Worker は何も保持せず、
 * runtime がブラウザと Modal を直結する。
 */
async function proxyWebSocket(
  request: Request,
  env: Env,
  upstreamUrl: URL
): Promise<Response> {
  const headers = buildUpstreamHeaders(request, env);

  // ブラウザのハンドシェイク用ヘッダーは上流へ持ち越さない。
  //
  // workerd は `Upgrade: websocket` を見て**自前で**ハンドシェイクを行う。
  // ブラウザの Sec-WebSocket-Key / Version / Extensions をコピーすると
  // それと衝突し、101 は返るのにフレームが流れないソケットができあがる。
  // Cloudflare の例が `headers: { Upgrade: "websocket" }` だけを渡しているのも同じ理由。
  headers.delete("Sec-WebSocket-Key");
  headers.delete("Sec-WebSocket-Version");
  headers.delete("Sec-WebSocket-Extensions");
  headers.delete("Sec-WebSocket-Accept");
  headers.delete("Connection");
  headers.set("Upgrade", "websocket");

  const upstream = await fetch(
    new Request(upstreamUrl, {
      method: request.method,
      headers,
      // 既定の "follow" だと、Modal が別オリジンへ 3xx を返したときに
      // Modal-Key / Modal-Secret を付けたまま追従してしまう。
      // 3xx はそのままブラウザへ返す。
      redirect: "manual",
    })
  );

  if (!upstream.webSocket) {
    // Modal が 101 を返さなかった場合（Proxy Auth 失敗時の 401 など）。
    console.warn(
      `WebSocket upgrade did not produce a socket: upstream status=${upstream.status}`
    );
  }

  return upstream;
}

/**
 * Location が Modal のホスト名を指す絶対 URL だった場合に、リクエスト元の
 * オリジンへ書き換える。ComfyUI の末尾スラッシュリダイレクト等で
 * .modal.run が露出するのを防ぐ。
 */
function rewriteLocationHeader(
  response: Response,
  modalOrigin: string,
  requestOrigin: string
): Response {
  const location = response.headers.get("Location");
  if (!location) {
    return response;
  }

  let target: URL;
  try {
    // 相対 Location はそのままで問題ないので、絶対 URL のときだけ処理する。
    target = new URL(location);
  } catch {
    return response;
  }

  if (target.origin !== new URL(modalOrigin).origin) {
    return response;
  }

  const rewritten = new URL(requestOrigin);
  rewritten.pathname = target.pathname;
  rewritten.search = target.search;
  rewritten.hash = target.hash;

  const headers = new Headers(response.headers);
  headers.set("Location", rewritten.toString());
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function textResponse(body: string, status: number): Response {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // 設定に不備がある状態で素通しさせない。HTTP と WebSocket の両経路が
    // この 1 か所を通るので、検証はここだけで足りる。
    const configError = findConfigError(env);
    if (configError) {
      console.error(configError);
      return textResponse("Proxy is not configured.", 500);
    }

    if (!(await isAuthenticated(request, env))) {
      return textResponse("Cloudflare Access authentication required.", 403);
    }

    const requestUrl = new URL(request.url);
    const upstreamUrl = upstreamUrlFor(requestUrl, env.MODAL_ORIGIN);

    if (isWebSocketUpgrade(request)) {
      return proxyWebSocket(request, env, upstreamUrl);
    }

    const upstream = await fetch(
      new Request(upstreamUrl, {
        method: request.method,
        headers: buildUpstreamHeaders(request, env),
        body: request.body,
        redirect: "manual",
      })
    );

    return rewriteLocationHeader(upstream, env.MODAL_ORIGIN, requestUrl.origin);
  },
} satisfies ExportedHandler<Env>;
