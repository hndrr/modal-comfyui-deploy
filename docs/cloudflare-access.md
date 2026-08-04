# Cloudflare Access で ComfyUI を保護する

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

`comfyapp.py` はこの設定をすでに環境変数として持っている（[`_resolve_requires_proxy_auth`](../comfyapp.py)）ため、Python コードの変更は不要で、`.env` の `COMFYUI_REQUIRES_PROXY_AUTH` を `on` にするだけでよい。

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

### 1. Access の JWT を検証する（fail-closed）

`Cf-Access-Jwt-Assertion` ヘッダー、無ければ `CF_Authorization` クッキーからトークンを取り出し、`jose` で検証する。

- JWKS: `${TEAM_DOMAIN}/cdn-cgi/access/certs`
- `issuer` = `TEAM_DOMAIN`
- `audience` = `POLICY_AUD`（Access アプリの Application Audience タグ）
- トークンが無い、または検証に失敗した場合は **403** を返し、Modal へは一切転送しない

Cloudflare のドキュメントは、オリジンがすでに公開されている構成では Access の迂回を防ぐためにオリジン側でトークンを検証するよう求めている。この構成では Worker がオリジンにあたるため、検証責任は Worker にある。これにより `workers_dev` の設定漏れやルート誤設定があっても閉じたままになる。

許可メールアドレスの判定は Access ポリシー側に一本化し、Worker には書かない。設定を二重管理しないためである。

### 2. Modal-Key / Modal-Secret を付与する

受け取った URL のパスとクエリをそのまま `MODAL_ORIGIN` へ引き継ぎ、`Modal-Key` と `Modal-Secret` を `set` で付ける。`set` なので、クライアントが同名ヘッダーを送ってきても上書きされる。

リクエストボディはバッファリングせずストリームのまま透過させる。

### 3. WebSocket を透過する

ComfyUI はブラウザとの状態同期に `/ws` を使うため、`Upgrade: websocket` のリクエストを転送できる必要がある。

```ts
const upstream = await fetch(upstreamRequest);
if (upstream.webSocket) {
  return new Response(null, { status: 101, webSocket: upstream.webSocket });
}
return upstream;
```

パススルーなので Worker 側で `accept()` は呼ばない。呼ぶと Worker が接続を終端してしまう。

`new Request(url, request)` が `Upgrade` ヘッダーを保持するかは runtime の挙動に依存するため、アップグレード要求を検出したらアップストリームのリクエストに `Upgrade: websocket` を明示的に再設定する。

### 4. Location ヘッダーを書き換える

`redirect: "manual"` で転送し、`Location` が `MODAL_ORIGIN` を指す絶対 URL だった場合はリクエスト元の origin に書き換える。ComfyUI の末尾スラッシュリダイレクト等で Modal のホスト名が漏れるのを防ぐ。

## 設定値

`worker/wrangler.jsonc` の `vars` に置くもの。秘密情報ではない。

| 変数 | 例 | 取得元 |
| --- | --- | --- |
| `MODAL_ORIGIN` | `https://<workspace>--comfyui-ui.modal.run` | `uv run modal deploy comfyapp.py` の出力、または `uv run modal app list` |
| `TEAM_DOMAIN` | `https://<team-name>.cloudflareaccess.com` | Zero Trust → Settings → Custom Pages の Team domain |
| `POLICY_AUD` | 64 桁の 16 進文字列 | Access アプリの Overview にある Application Audience (AUD) Tag |

`wrangler secret put` で登録するもの。**`wrangler.jsonc` やソースコードに直接書かない。**

| シークレット | 形式 | 取得元 |
| --- | --- | --- |
| `MODAL_KEY` | `wk-` 始まり | Modal ダッシュボード → Settings → Proxy Auth Tokens の Token ID |
| `MODAL_SECRET` | `ws-` 始まり | 同上の Token Secret（作成時のみ表示される） |

ローカルで `wrangler dev` を動かす場合は `worker/.dev.vars`（gitignore 済み）に `MODAL_KEY` / `MODAL_SECRET` を書く。`worker/.dev.vars.example` をコピーして使う。

## セットアップ手順

**この順序で実行する。** 途中で無防備な URL が公開される時間帯を作らないための順序になっている。`comfy.example.com` の DNS レコードは手順 6 で初めて作られるため、それ以前にホスト名へ到達する経路は存在しない。

### 1. Modal の Proxy Auth トークンを作成する

Modal ダッシュボード → Settings → Proxy Auth Tokens → 新規作成。

Token ID（`wk-` 始まり）と Token Secret（`ws-` 始まり）を控える。Secret は作成時にしか表示されない。

### 2. Modal 側を Proxy Auth 必須にする

`.env` に次を書く。

```bash
COMFYUI_REQUIRES_PROXY_AUTH=on
```

デプロイする。

```bash
uv run modal deploy comfyapp.py
```

直 URL が閉じたことを確認する。

```bash
# 401 になること
curl -sS -o /dev/null -w '%{http_code}\n' https://<workspace>--comfyui-ui.modal.run/

# 200 になること
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H 'Modal-Key: wk-...' -H 'Modal-Secret: ws-...' \
  https://<workspace>--comfyui-ui.modal.run/
```

この設定はデプロイ時に決まるため、変更したら再デプロイが必要になる。

### 3. Google を Identity provider として追加する

One-time PIN のみでよい場合はこの手順を飛ばせる。One-time PIN は Cloudflare 標準機能で、既定で有効になっている。

Google Cloud Console で OAuth 2.0 クライアント ID を作成する。

- 承認済みのリダイレクト URI: `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/callback`

Cloudflare Zero Trust → Settings → Authentication → Login methods → Add new → Google に、発行された Client ID と Client secret を登録する。Test を実行して成功することを確認する。

### 4. Access アプリケーションを作成する

Zero Trust → Access controls → Applications → Create new application → **Self-hosted and private** → **Add public hostname**。

- Public hostname: `comfy.example.com`（Cloudflare で管理しているゾーンのサブドメイン）
- Session Duration: 毎回ログインし直さずに済む長さにする（例: 24 時間、1 週間）
- Login methods: One-time PIN と Google を有効にする
- Policy:
  - Action: `Allow`
  - Include: `Emails` → 自分のメールアドレス（複数可）

作成後、アプリの Overview にある **Application Audience (AUD) Tag** をコピーする。手順 5 の `POLICY_AUD` に使う。

### 5. Worker を設定する

```bash
cd worker
npm install
npx wrangler secret put MODAL_KEY      # wk-... を貼り付け
npx wrangler secret put MODAL_SECRET   # ws-... を貼り付け
```

`worker/wrangler.jsonc` の次の箇所を実際の値に書き換える。

- `routes[0].pattern`: `comfy.example.com`
- `vars.MODAL_ORIGIN`
- `vars.TEAM_DOMAIN`
- `vars.POLICY_AUD`

### 6. Worker をデプロイする

```bash
npx wrangler deploy
```

Custom Domain として登録されるため、DNS レコードと証明書は Cloudflare が自動で作成する。

ブラウザで `https://comfy.example.com` を開き、Cloudflare のログイン画面 → 認証 → ComfyUI が表示されることを確認する。

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

## 制約と運用上の注意

### アップロードサイズの上限

Cloudflare を経由するリクエストにはサイズ上限がある。

| Cloudflare プラン | 1 リクエストの最大アップロード |
| --- | ---: |
| Free / Pro | 100MB |
| Business | 200MB |
| Enterprise | 500MB 以上 |

通常の画像入力では問題になりにくいが、100MB を超える動画やモデルファイルを ComfyUI の画面からアップロードすると Free / Pro では `413 Payload Too Large` になる。

大きいファイルは Cloudflare を経由させず、[ComfyUI 資産の管理画面](../README.md#4-comfyui-資産を管理する)（`web/`）を使ってローカルから Modal Volume へ直接アップロードする。管理画面は `asset_rpc.py` 経由で Modal SDK を直接呼ぶため、この構成の影響を受けない。

### コールドスタートと 524

Cloudflare はオリジンの応答を約 100 秒待って `524` を返す。ComfyUI は `min_containers=0` でゼロ台まで縮退するため、縮退後の初回アクセスではコンテナ起動を待つことになる。`startup_timeout=60` にイメージ取得時間が加わり 100 秒を超えると `524` になり得る。

その場合はページを再読み込みすればよい。2 回目はコンテナが起動済みなので通常どおり表示される。

### scale-to-zero への影響

Worker は自前の常時接続を持たないため、[アイドル時の scale-to-zero](modal-idle-scale-to-zero.md) の挙動は変わらない。ComfyUI のタブを閉じれば従来どおり `scaledown_window` の範囲でコンテナが縮退する。

### 将来の Sleep/Wake エンドポイント

[Modal 電源コントロール](modal-power-control.md)で構想している `/modal/power/*` は ComfyUI と同一オリジン配下のパスなので、Worker をそのまま素通しで通る。Worker 側の追加対応は不要である。

### 認証情報のローテーション

Modal の Proxy Auth トークンを作り直した場合は、`wrangler secret put` で `MODAL_KEY` / `MODAL_SECRET` を上書きしてから `wrangler deploy` する。Modal 側の再デプロイは不要である。

## トラブルシューティング

| 症状 | 原因の切り分け |
| --- | --- |
| `comfy.example.com` が 403 | Access は通過したが Worker の JWT 検証に失敗している。`POLICY_AUD` と `TEAM_DOMAIN` が Access アプリの値と一致しているか確認する。`npx wrangler tail` でログを見る |
| `comfy.example.com` が 401 | Worker が付けた `Modal-Key` / `Modal-Secret` を Modal が拒否している。シークレットが正しく登録されているか（`npx wrangler secret list`）、Modal 側でトークンが失効していないか確認する |
| ログイン画面が出ずに ComfyUI が表示される | Access アプリのホスト名が Worker のホスト名と一致していない。Access アプリの設定を確認する |
| 生成の進捗が更新されない | `/ws` の WebSocket が繋がっていない。DevTools → Network → WS で `101 Switching Protocols` になっているか確認する。なっていなければ Worker の Upgrade ヘッダー転送を見直す |
| アップロードで 413 | Cloudflare のリクエストサイズ上限。管理画面（`web/`）から Volume へ直接アップロードする |
| 初回アクセスで 524 | Modal のコールドスタートが 100 秒を超えている。再読み込みする |
| Modal の直 URL が 200 で開ける | `COMFYUI_REQUIRES_PROXY_AUTH=on` にした後の再デプロイを忘れている。`uv run modal deploy comfyapp.py` を実行する |

## 参考

- [Cloudflare One — Publish a self-hosted application to the Internet](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Cloudflare One — Add web applications](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/)
- [Cloudflare One — Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
- [Cloudflare Workers — Using the WebSockets API](https://developers.cloudflare.com/workers/examples/websockets/)
- [Cloudflare Workers — workers.dev](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/)
- [Cloudflare Support — Error 413](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/)
- [Modal — Proxy Tokens](https://modal.com/docs/guide/webhook-proxy-auth)
