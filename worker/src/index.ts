import { createRemoteJWKSet, jwtVerify } from "jose";

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

/**
 * JWKS の取得結果は jose がキャッシュする。isolate をまたいで再利用したいので
 * モジュールスコープに保持し、TEAM_DOMAIN が変わったときだけ作り直す。
 */
let cachedJwks: ReturnType<typeof createRemoteJWKSet> | undefined;
let cachedJwksTeamDomain: string | undefined;

function getJwks(teamDomain: string): ReturnType<typeof createRemoteJWKSet> {
  if (!cachedJwks || cachedJwksTeamDomain !== teamDomain) {
    cachedJwks = createRemoteJWKSet(
      new URL(`${teamDomain}/cdn-cgi/access/certs`)
    );
    cachedJwksTeamDomain = teamDomain;
  }
  return cachedJwks;
}

function normalizeTeamDomain(raw: string): string {
  return raw.trim().replace(/\/+$/, "");
}

function findMissingConfigKeys(env: Env): string[] {
  return REQUIRED_CONFIG_KEYS.filter((key) => !env[key]?.trim());
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
    return false;
  }

  const teamDomain = normalizeTeamDomain(env.TEAM_DOMAIN);
  try {
    await jwtVerify(token, getJwks(teamDomain), {
      issuer: teamDomain,
      audience: env.POLICY_AUD.trim(),
    });
    return true;
  } catch {
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
  // クライアントが偽装したヘッダーがあっても set で上書きされる。
  headers.set("Modal-Key", env.MODAL_KEY);
  headers.set("Modal-Secret", env.MODAL_SECRET);
  return headers;
}

function isWebSocketUpgrade(request: Request): boolean {
  return request.headers.get("Upgrade")?.toLowerCase() === "websocket";
}

/**
 * ComfyUI の /ws を透過する。
 *
 * パススルーなので accept() は呼ばない。呼ぶとこの Worker が接続を終端して
 * しまい、ブラウザまでフレームが届かなくなる。
 */
async function proxyWebSocket(
  request: Request,
  env: Env,
  upstreamUrl: URL
): Promise<Response> {
  const headers = buildUpstreamHeaders(request, env);
  // Headers をコピーし直した際に Upgrade が落ちる runtime があるため明示的に付け直す。
  headers.set("Upgrade", "websocket");

  const upstream = await fetch(
    new Request(upstreamUrl, { method: request.method, headers })
  );

  const webSocket = upstream.webSocket;
  if (!webSocket) {
    // Modal 側が 101 を返さなかった場合（401 など）はそのまま返してブラウザに見せる。
    return upstream;
  }

  const responseHeaders = new Headers();
  const protocol = upstream.headers.get("Sec-WebSocket-Protocol");
  if (protocol) {
    responseHeaders.set("Sec-WebSocket-Protocol", protocol);
  }

  return new Response(null, { status: 101, webSocket, headers: responseHeaders });
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
    const missing = findMissingConfigKeys(env);
    if (missing.length > 0) {
      // 設定が欠けている状態で素通しさせない。
      console.error(`Missing configuration: ${missing.join(", ")}`);
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
