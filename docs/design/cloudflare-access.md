# Cloudflare Access + Worker の設計

手順は [../cloudflare-access.md](../cloudflare-access.md) を参照。

## 目的

Modal 上の ComfyUI に、ComfyUI 本体にも Modal にもログイン機能を追加せずにログイン画面を付ける。

Cloudflare Access を ComfyUI の手前に置き、許可したメールアドレスの利用者だけを通す。認証方式はメールのワンタイム PIN と Google ログインを使う。

## 構成

```text
ブラウザ
  ↓ https://comfy.example.com
Cloudflare Access
  ├─ One-time PIN（メールに届く6桁コード）
  ├─ Google ログイン
  └─ Allow ポリシーに一致するメールアドレスか判定
  ↓ CF_Authorization クッキー / Cf-Access-Jwt-Assertion ヘッダー
Cloudflare Worker（リバースプロキシ）
  ├─ Access の JWT を検証（不正・不在なら 403）
  └─ Modal-Key / Modal-Secret を付与
  ↓
Modal 上の ComfyUI（requires_proxy_auth=on、コンテナ内ポート 8000）
```

Cloudflare Access はリクエストごとに認証済みセッションを確認し、Allow ポリシーに一致した利用者だけを先へ通す。Access は Allow に一致しないリクエストを既定で拒否する。

Modal はすでにインターネットへ公開されているため、自宅サーバーのように Cloudflare Tunnel を立てる必要はない。Cloudflare も、オリジンがすでに公開されている場合は Tunnel が必須ではないとしている。

## なぜ Modal の直 URL も閉じる必要があるか

Cloudflare Access を付けただけでは、元の

```text
https://<workspace>--comfyui-ui.modal.run
```

を直接開くことで Cloudflare を迂回できてしまう。そのため次の二重構成にする。

1. Modal 側を Proxy Auth 必須（`requires_proxy_auth=True`）にし、直 URL を 401 にする
2. Cloudflare Worker だけが `Modal-Key` / `Modal-Secret` を付けて Modal へ接続する

`comfyapp.py` はこの設定をすでに環境変数として持っている（[`_resolve_requires_proxy_auth`](../../comfyapp.py)）ため、Python コードの変更は不要で、`.env` の `COMFYUI_REQUIRES_PROXY_AUTH` を `on` にするだけでよい。

```python
@modal.web_server(
    8000,
    startup_timeout=60,
    requires_proxy_auth=REQUIRES_PROXY_AUTH,  # COMFYUI_REQUIRES_PROXY_AUTH=on で True
)
def ui():
    ...
```

Modal の Proxy Auth は `fastapi_endpoint` / `asgi_app` / `wsgi_app` / `web_server` に対応する。

## Worker の責務

`worker/src/index.ts` は次の 4 つだけを行う。ComfyUI のパスや API を解釈しない、素通しのリバースプロキシとする。

前提として、必要な設定値（`MODAL_ORIGINS` / `TEAM_DOMAIN` / `POLICY_AUD` / `MODAL_KEY` / `MODAL_SECRET`）が 1 つでも欠けている場合は、転送せずに **500** を返す。設定漏れのまま素通しさせないためである。

### 1. Access の JWT を検証する（fail-closed）

`Cf-Access-Jwt-Assertion` ヘッダー、無ければ `CF_Authorization` クッキーからトークンを取り出し、`jose` で検証する。

- JWKS: `${TEAM_DOMAIN}/cdn-cgi/access/certs`
- `issuer` = `TEAM_DOMAIN`
- `audience` = `POLICY_AUD`（Access アプリの Application Audience タグ）
- トークンが無い、または検証に失敗した場合は **403** を返し、Modal へは一切転送しない

Cloudflare のドキュメントは、オリジンがすでに公開されている構成では Access の迂回を防ぐためにオリジン側でトークンを検証するよう求めている。この構成では Worker がオリジンにあたるため、検証責任は Worker にある。これにより `workers_dev` の設定漏れやルート誤設定があっても閉じたままになる。

許可メールアドレスの判定は Access ポリシー側に一本化し、Worker には書かない。設定を二重管理しないためである。

### 2. Modal-Key / Modal-Secret を付与する

受け取った URL のパスとクエリを、ホスト名から解決した Modal オリジンへ引き継ぎ、`Modal-Key` と `Modal-Secret` を `set` で付ける。`set` なので、クライアントが同名ヘッダーを送ってきても上書きされる。

あわせて次の 3 つを落とす。

- `Host`: 受け取った値（`comfy.example.com`）を持ち越すと Modal がルーティングできないため、転送先 URL から runtime に決めさせる
- `Cf-Access-Jwt-Assertion`: 検証済みでこの先は使わないため、ComfyUI へ渡さない
- `Cookie` の `CF_Authorization`: **Access の JWT はクッキーでも届くため、ヘッダーだけ消しても素通りする。** `CF_Authorization` だけを取り除き、他のクッキーはそのまま残す（`stripAccessCookie`）。残り 0 個になった場合は `Cookie` ヘッダーごと削除する

リクエストボディはバッファリングせずストリームのまま透過させる。

### 3. WebSocket を透過する

ComfyUI はブラウザとの状態同期に `/ws` を使う。ここが構成全体で一番はまりやすい。

```ts
const headers = buildUpstreamHeaders(request, env);

// ブラウザのハンドシェイク用ヘッダーは持ち越さない
headers.delete("Sec-WebSocket-Key");
headers.delete("Sec-WebSocket-Version");
headers.delete("Sec-WebSocket-Extensions");
headers.delete("Connection");
headers.set("Upgrade", "websocket");

const upstream = await fetch(
  new Request(upstreamUrl, {
    method: "GET",
    headers,
    // 既定の "follow" だと 3xx に追従して Modal-Key / Modal-Secret が
    // 転送先へ渡ってしまう
    redirect: "manual",
  })
);

// upstream の Response をそのまま返す
return upstream;
```

**`Sec-WebSocket-*` を上流へコピーしないこと。**
workerd は `Upgrade: websocket` を見て自前でハンドシェイクを行う。ブラウザの `Sec-WebSocket-Key` などをコピーするとそれと衝突し、`101 Switching Protocols` は返るのに 1 フレームも流れないソケットができる。Cloudflare の例が `headers: { Upgrade: "websocket" }` だけを渡しているのはこのためである。

この構成で WebSocket が動かなかった原因はこれだった。ヘッダーをコピーしたままだと、後述のどの返し方に変えても症状は変わらなかった。

**upstream の Response をそのまま返す。**
Worker はソケットを保持せず、runtime がブラウザと Modal を直結するため、中継コードを書かずに済む。単一接続をそのまま通すだけならこれで足りる。

Modal が 101 以外（Proxy Auth 失敗時の 401 など）を返した場合は、そのレスポンスがそのままブラウザへ返る。

> **`WebSocketPair` を使う中継実装について**
>
> フレームを加工したい、接続ごとに状態を持ちたいといった理由で Worker が能動的に中継したい場合は、`WebSocketPair` で対を作り、`fetch()` が返したソケットとの間で双方向にフレームを転送する。その際は Close フレームの調停のために `accept({ allowHalfOpen: true })` を使う。**単一接続を中継するだけなら Durable Object は不要。** Durable Object が要るのは、接続状態を保持したい場合や複数接続を協調させたい場合である。
>
> このリポジトリでは加工が不要なので、中継せず素通しする実装を採用している。

なお **Modal の Proxy Auth は WebSocket でも問題なく動く**（公式ドキュメントには明記がないが、`Modal-Key` / `Modal-Secret` 付きで 101 が返り、接続も維持されることを実測で確認した）。

#### 切れているかどうかの見分け方

- ブラウザの DevTools → Network → WS で、**`/ws?clientId=...` とクエリ付きで再接続していれば正常**。ComfyUI は初回接続で受け取った `sid` を以降 `clientId` として付けるため、クエリ無しの `/ws` が 1 秒間隔で並んでいたらフレームが 1 つも届いていない。
- Worker のログ（`npm run tail` または observability）で `GET /ws` の `wallTimeMs` を見る。接続が維持されていれば実行は終わらないので**完了イベント自体が出ない**。数百 ms で完了し続けている場合は切れている。

### 4. Location ヘッダーを書き換える

`redirect: "manual"` で転送し、`Location` が転送先の Modal オリジンを指す絶対 URL だった場合はリクエスト元の origin に書き換える。ComfyUI の末尾スラッシュリダイレクト等で Modal のホスト名が漏れるのを防ぐ。

## 迂回経路を塞ぐ

独自ドメインだけを Access で保護しても、Worker の `workers.dev` URL や Preview URL が公開されたままだとそこから Worker へ直接入れてしまう。`worker/wrangler.jsonc` では次を必ず有効にしておく。

```jsonc
{
  "workers_dev": false,
  "preview_urls": false
}
```

Cloudflare も、Worker をカスタムドメインだけからアクセス可能にする場合は `workers.dev` と Preview URL の両方を無効にするよう案内している。

これらを外すと迂回経路が復活する。ただし Worker 側でも Access の JWT を検証しているため、設定が外れた場合でも Worker は 403 を返して fail-closed になる。二重の防御として両方を維持する。

## scale-to-zero への影響

Worker は自前の常時接続を持たないため、[アイドル時の scale-to-zero](modal-idle-scale-to-zero.md) の挙動は変わらない。ComfyUI のタブを閉じれば従来どおり `scaledown_window` の範囲でコンテナが縮退する。

## 将来の Sleep/Wake エンドポイント

[Modal 電源コントロール](modal-power-control.md)で構想している `/modal/power/*` は ComfyUI と同一オリジン配下のパスなので、Worker をそのまま素通しで通る。Worker 側の追加対応は不要である。

## 参考

- [Cloudflare One — Publish a self-hosted application to the Internet](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Cloudflare One — Add web applications](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/)
- [Cloudflare One — Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
- [Cloudflare Workers — Using the WebSockets API](https://developers.cloudflare.com/workers/examples/websockets/)
- [Cloudflare Workers — workers.dev](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/)
- [Cloudflare Support — Error 413](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/)
- [Modal — Proxy Tokens](https://modal.com/docs/guide/webhook-proxy-auth)
