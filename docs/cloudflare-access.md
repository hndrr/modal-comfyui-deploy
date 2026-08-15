# Cloudflare Access で ComfyUI を保護する

Modal 上の ComfyUI の手前に Cloudflare Access を置き、許可した利用者だけを通す構成の手順。
なぜこの構成なのか、Worker が何をしているかは [design/cloudflare-access.md](design/cloudflare-access.md) を参照。

## 設定値

**`worker/wrangler.jsonc` には環境固有の値を一切書かない。** リポジトリを公開しても、ワークスペース名・ドメイン・Team domain が漏れないようにするためである。

そのため設定値は 5 つとも `wrangler secret put` で登録する。secret は Cloudflare 側に保存され、設定ファイルに書かれていなくてもデプロイで消えない（Cloudflare のドキュメントに *Secrets not included in the file are preserved from the previous version* と明記されている）。

| 名前 | 例 / 形式 | 取得元 |
| --- | --- | --- |
| `MODAL_ORIGINS` | ホスト名 → オリジンの JSON マップ（下記） | `uv run modal deploy ...` の出力。ワークスペース名は `uv run modal profile current` で確認できる |
| `TEAM_DOMAIN` | `https://<team-name>.cloudflareaccess.com` | Zero Trust → Settings → Custom Pages の Team domain |
| `POLICY_AUD` | 64 桁の 16 進文字列 | Access アプリの Overview にある Application Audience (AUD) Tag |
| `MODAL_KEY` | `wk-` 始まり | Modal ダッシュボード → Settings → Proxy Auth Tokens の Token ID |
| `MODAL_SECRET` | `ws-` 始まり | 同上の Token Secret（作成時のみ表示される） |

`MODAL_ORIGINS` / `TEAM_DOMAIN` / `POLICY_AUD` は秘密情報というより「リポジトリに置きたくない環境固有の値」である。`wrangler dev` / `wrangler deploy` には `--var key:value` があるのでコマンドラインからも渡せるが、値がシェル履歴に残るうえ毎回指定が必要になるため、デプロイ用の値は secret に寄せている。

### MODAL_ORIGINS — 1 つの Worker で複数の Modal app を出す

`MODAL_ORIGINS` は**公開ホスト名から転送先 Modal オリジンへのマップ**である。ホスト名も Modal の URL も環境固有なので、両方まとめてこの secret に入れることでリポジトリから追い出している。

URL の `<workspace>` はワークスペース名なので、**別の Modal アカウントでデプロイし直した場合は `MODAL_ORIGINS` と Proxy Auth トークン（`MODAL_KEY` / `MODAL_SECRET`）を入れ直す**必要がある。アカウントの切り替え自体は [modal-profiles.md](modal-profiles.md) を参照。

```json
{
  "comfy.example.com": "https://<workspace>--comfyui-ui.modal.run",
  "model.example.com": "https://<workspace>--preserve-model-web.modal.run"
}
```

Worker はリクエストの `Host` を見て転送先を決める。app を増やしたいときは、この JSON にエントリを足し、ホスト名を Custom Domain として割り当て、Access アプリの `destinations` に追加すればよい。**Worker のコード変更は不要。**

検証は起動時に行い、次のいずれかに当たると転送せず **500** を返す。

- JSON として壊れている / オブジェクトでない / エントリが 0 件
- 値が URL として不正、または **https でない**（http へ転送すると `Modal-Key` / `Modal-Secret` が平文経路に載るため）
- リクエストのホスト名がマップに無い（Custom Domain は明示的に割り当てるものなので、これは設定の追加漏れを意味する）

接続先ホスト名（`comfy.example.com`）も `wrangler.jsonc` に書かず、Cloudflare ダッシュボードで Custom Domain として登録する。Cloudflare のドキュメントは *To manage routes via the Cloudflare dashboard only, remove any route and routes keys from your Wrangler configuration file* としており、`routes` キーを持たない設定ファイルはダッシュボード側の設定を上書きしない。

ローカルで `wrangler dev` を動かす場合は、同じ 5 つを `worker/.dev.vars`（gitignore 済み）に書く。`worker/.dev.vars.example` をコピーして使う。

```bash
cd worker
cp .dev.vars.example .dev.vars
```

### npm スクリプト

`worker/` で使えるコマンド。

| コマンド | 内容 |
| --- | --- |
| `npm run dev` | ローカルで Worker を起動する（`wrangler dev`） |
| `npm run deploy` | Cloudflare へデプロイする（`wrangler deploy`） |
| `npm run tail` | 本番の Worker のログを追う（`wrangler tail`） |
| `npm run typecheck` | 型チェック（`tsc --noEmit`） |

## セットアップ手順

**この順序で実行する。** Cloudflare 側を先に完成させ、経路が通ったことを確認してから Modal の直 URL を閉じる。

こうすると切り替え中も ComfyUI を使い続けられる。Worker を先にデプロイしても、Modal がまだ Proxy Auth を要求していないだけで Worker 自体は正常に動くため、最後の手順 8 で無停止に切り替わる。

Worker は最初のデプロイ時点ではホスト名を持たず、`workers.dev` と Preview URL も無効なので、手順 7 まで外部から到達できる経路は存在しない。

### 1. Modal の Proxy Auth トークンを作成する

Modal ダッシュボード → Settings → Proxy Auth Tokens → 新規作成。

Token ID（`wk-` 始まり）と Token Secret（`ws-` 始まり）を控える。Secret は作成時にしか表示されない。

この時点では Modal 側の設定は変更しない。ComfyUI は従来どおり直 URL で使える。

### 2. 接続先の URL を確認する

Worker に渡す Modal のオリジンを控える。

```bash
uv run modal profile current   # ワークスペース名
uv run modal app list          # comfyui が deployed であること
```

URL は `https://<workspace>--comfyui-ui.modal.run` になる。

> ComfyUI の URL に直接 `curl` を投げると GPU コンテナがコールドスタートして課金対象になる。到達確認は手順 8 の 401 チェックで行えばよい（401 は Modal のエッジが返すため、コンテナは起動しない）。

### 3.（任意）Google を Identity provider として追加する

**この手順は省略できる。** One-time PIN は Cloudflare 標準機能で既定から使えるため、個人利用ならこれだけで足りる。実際の構築でも One-time PIN のみで運用している。

Google ログインも使いたくなった場合は、後から追加しても Access アプリを作り直す必要はない（IdP を登録し、アプリの Login methods に足すだけ）。

Google Cloud Console で OAuth 2.0 クライアント ID を作成する。

- 承認済みのリダイレクト URI: `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/callback`

Cloudflare Zero Trust → Settings → Authentication → Login methods → Add new → Google に、発行された Client ID と Client secret を登録する。Test を実行して成功することを確認する。

IdP が 1 つだけの場合は、Access アプリ側で `auto_redirect_to_identity` を有効にすると IdP 選択画面を飛ばせる。複数登録するとこの設定は使えない。

### 4. Access アプリケーションを作成する

Zero Trust → Access controls → Applications → Create new application → **Self-hosted and private** → **Add public hostname**。

- Public hostname: `comfy.example.com`（Cloudflare で管理しているゾーンのサブドメイン）
- Session Duration: 毎回ログインし直さずに済む長さにする（例: 24 時間、1 週間）
- Login methods: One-time PIN と Google を有効にする
- Policy:
  - Action: `Allow`
  - Include: `Emails` → 自分のメールアドレス（複数可）

作成後、アプリの Overview にある **Application Audience (AUD) Tag** をコピーする。手順 6 の `POLICY_AUD` に使う。

### 5. Worker をデプロイする

Cloudflare にログインし、Worker を先にデプロイする。この時点ではホスト名が未設定なので、外部から到達できる経路は無い。

```bash
cd worker
npm install
npx wrangler login
npx wrangler deploy
```

### 6. 設定値を secret として登録する

`wrangler.jsonc` は編集しない。5 つとも対話入力で登録する。

```bash
npx wrangler secret put MODAL_ORIGINS   # {"comfy.example.com":"https://..."} を 1 行で
npx wrangler secret put TEAM_DOMAIN     # https://<team-name>.cloudflareaccess.com
npx wrangler secret put POLICY_AUD      # 手順 4 でコピーした AUD タグ
npx wrangler secret put MODAL_KEY       # wk-... を貼り付け
npx wrangler secret put MODAL_SECRET    # ws-... を貼り付け
```

`wrangler secret put` は登録のたびに新しいバージョンを作ってデプロイするため、この後の `wrangler deploy` は不要である。

登録済みの名前は次で確認できる（値は表示されない）。

```bash
npx wrangler secret list
```

### 7. ホスト名を割り当てる

Cloudflare ダッシュボード → Workers & Pages → `comfyui-access-proxy` → Settings → Domains & Routes → Add → Custom Domain で `comfy.example.com` を登録する。

DNS レコードと証明書は Cloudflare が自動で作成する。`wrangler.jsonc` に `routes` キーが無いため、以降の `wrangler deploy` でこの設定が上書きされることはない。

ブラウザで `https://comfy.example.com` を開き、Cloudflare のログイン画面 → 認証 → ComfyUI が表示されることを確認する。

### 8. Modal 直 URL を閉じる

Cloudflare 経由で ComfyUI が開けることを確認してから、最後に直 URL を閉じる。

`.env` に次を書く。

```bash
COMFYUI_REQUIRES_PROXY_AUTH=on
```

デプロイする。この設定はデプロイ時に決まるため、変更したら再デプロイが必要になる。

```bash
uv run modal deploy comfyapp.py
```

直 URL が閉じ、Worker 経由は通ることを確認する。

```bash
# 401 になること（Modal のエッジが返すためコンテナは起動しない）
curl -sS -o /dev/null -w '%{http_code}\n' https://<workspace>--comfyui-ui.modal.run/

# 200 になること
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H 'Modal-Key: wk-...' -H 'Modal-Secret: ws-...' \
  https://<workspace>--comfyui-ui.modal.run/
```

最後にブラウザで `https://comfy.example.com` を再読み込みし、ComfyUI がそのまま使えることを確認する。

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

Cloudflare は既定の Proxy Read Timeout である **125 秒**だけオリジンの応答を待ち、超えると `524` を返す（Enterprise プランのみ Cache Rules または zone setting API で最大 6,000 秒まで延長できる）。

ComfyUI は `min_containers=0` でゼロ台まで縮退するため、縮退後の初回アクセスではコンテナ起動を待つことになる。イメージ取得を含む起動時間が 125 秒を超えると `524` になり得る。

なお `startup_timeout=60`（[comfyapp.py:349](../comfyapp.py#L349)）は **Modal がコンテナ内の Web サーバーの起動を待つ上限**であり、Cloudflare のタイムアウトとは別物である。両者は独立して効く。

その場合はページを再読み込みすればよい。2 回目はコンテナが起動済みなので通常どおり表示される。

## 認証情報のローテーション

Modal の Proxy Auth トークンを作り直した場合、`MODAL_KEY` と `MODAL_SECRET` は **必ず 1 回のデプロイでまとめて更新する**。

`wrangler secret put` は実行ごとに新しいバージョンをデプロイするため、1 つずつ入れると「新しい Key と古い Secret」の組み合わせでデプロイされる瞬間が生まれ、その間 Modal が 401 を返す。

```bash
cd worker
cat > /tmp/modal-secrets.json <<'JSON'
{ "MODAL_KEY": "wk-...", "MODAL_SECRET": "ws-..." }
JSON
npx wrangler secret bulk /tmp/modal-secrets.json
rm /tmp/modal-secrets.json
```

`wrangler secret bulk` は複数の secret を 1 リクエストで更新する。`wrangler deploy --secrets-file <file>` でも同じことができる。Modal 側の再デプロイは不要である。

Modal の URL が変わった場合（ワークスペース名の変更など）は、単独の値なので `npx wrangler secret put MODAL_ORIGINS` で JSON ごと入れ直せばよい。

## トラブルシューティング

| 症状 | 原因の切り分け |
| --- | --- |
| `comfy.example.com` が 403 | **Access の拒否と Worker の検証失敗の両方で起こる。まず切り分ける。** Zero Trust → Logs → Access で該当リクエストの `allowed` を見るか、レスポンス本文を確認する（Cloudflare の拒否ページなら Access、`Cloudflare Access authentication required.` なら Worker）。Access が拒否した場合はリクエストが Worker に届かないので `npx wrangler tail` には何も出ない。Access を通過していた場合のみ、下の行へ進む |
| `comfy.example.com` が 401 | Worker が付けた `Modal-Key` / `Modal-Secret` を Modal が拒否している。シークレットが正しく登録されているか（`npx wrangler secret list`）、Modal 側でトークンが失効していないか確認する |
| ログイン画面が出ずに ComfyUI が表示される | Access アプリのホスト名が Worker のホスト名と一致していない。Access アプリの設定を確認する |
| 生成の進捗が更新されない | `/ws` が繋がっていない。DevTools → Network → WS で `/ws?clientId=...` と**クエリ付きの再接続**が出ているか確認する。クエリ無しの `/ws` が 1 秒間隔で並ぶ場合はフレームが届いていないので、「WebSocket を透過する」の `Sec-WebSocket-*` の扱いを見直す。なお WebSocket が繋がる前にキューへ入れた job は古い `client_id` 宛に進捗が送られるため、直した後は生成を新規に流し直して確認する |
| Access を通過した後に Worker が 403 を返す | Worker の JWT 検証に失敗している。`npm run tail` で `Access token rejected: ...` の理由が出るので、それを見る。`POLICY_AUD` が Access アプリの AUD と、`TEAM_DOMAIN` が JWT の `iss` と完全一致しているかを確認する（team domain を改名した後に secret を更新し忘れているとここで落ちる） |
| PWA manifest が CORS エラーになる | `<link rel="manifest">` は資格情報を送らずに取得されるため Access がログインへリダイレクトする。表示上のノイズで機能には影響しない |
| アップロードで 413 | Cloudflare のリクエストサイズ上限。管理画面（`web/`）から Volume へ直接アップロードする |
| 初回アクセスで 524 | Modal のコールドスタートが Cloudflare の Proxy Read Timeout（既定 125 秒）を超えている。再読み込みする |
| Modal の直 URL が 200 で開ける | `COMFYUI_REQUIRES_PROXY_AUTH=on` にした後の再デプロイを忘れている。`uv run modal deploy comfyapp.py` を実行する |
