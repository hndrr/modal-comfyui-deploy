# ComfyUI MCP の管理とローカル stdio の設定

すでに HTTPS の接続先がある場合は、[利用ガイド](split-mcp.md)から始めてください。このページは、split の管理者やローカルで stdio サーバーを起動したい人向けの手順です。

## HTTPS の接続先を用意する

1. split の CPU イメージに固定版 Node.js と MCP をデプロイする（`scripts/modal.sh deploy splitapp.py`）。GPU に Node/MCP は不要。既存 Worker をテスト後にデプロイする。一般の中継ルートでは内部呼び出し用ヘッダーを必ず取り除く。
2. split ホスト全体を従来のブラウザ・stdio 用 Access アプリに登録する。同じホストの `/mcp` に、より具体的なパスを持つ別の self-hosted Access アプリを設定し、Include が本人のメール1件のみの Allow を関連付ける。ブラウザ用共有アプリとは別の AUD を使う。Managed OAuth を有効にし、CLI のループバックと、ChatGPT が表示する**正確なコールバック URI**を許可する。トークンや本人メールは Git に記録しない。

   ChatGPT の登録フォームで動的クライアント登録（DCR）を選ぶと、コールバック URL が見えない場合がある。「OAuthの詳細設定 → 登録方法」で一度「ユーザー定義の OAuth クライアント」を選ぶと、このフォーム用の URL が表示される。それを Cloudflare の「許可されたリダイレクト URI」に追加して保存し、**同じフォームで DCR に戻して**プラグインを作成する。クライアント ID やシークレットの手入力、ワイルドカードの許可は不要。「接続」で Cloudflare に移ったら本人のログインを完了する。
3. Worker の secret `SPLIT_REMOTE_MCP` に次の JSON を対話入力する。既存の split ホストが解決する転送先と `origin` が一致しない限り中継されない。ホスト変更時は、既存の暗号化済みマップを置き換えずに `SPLIT_UI_ORIGIN` に `{"split.example.com":"https://example.modal.run"}` を設定できる。stdio の `SPLIT_MCP_ACCESS` とローカルの `COMFYUI_URL` も新ホストに合わせ、Worker の Custom Domain として登録する。

   ```json
   {"hostname":"split.example.com","split_hostname":"split.example.com","origin":"https://example.modal.run","audience":"<専用アプリのAUD>","owner_email":"owner@example.com"}
   ```

4. 認証なしでは OAuth のチャレンジとなること、別アプリの JWT では拒否されることを確認する。本人の OAuth で接続後、管理側から CPU/GPU の台数を読む。アプリの状態 API を定期照会すると CPU を起こすため、停止判定には使わない。

ChatGPT 添付が Azure Blob から配信される場合は、ChatGPT の添付で確認した配信ホストだけを `worker/` で `npx wrangler secret put SPLIT_ATTACHMENT_BLOB_HOSTS` にカンマ区切りで登録する。別のストレージアカウント、ワイルドカード、署名なしの Blob URL、リダイレクトは許可しない。配信ホストや署名付き URL は Git に残さない。

適用後は、Modal の管理画面で台数を見ながら、ツール一覧（CPU/GPU ともに0台）、モデル一覧（CPU のみ）、[小さな生成](../mcp/smoke-workflow.json)の順で確認する。生成には同じ `request_id` を使い、二重投入されないこと、出力を回収できること、監視終了後に両方0台へ戻ることを確認する。ジョブがなくても split の状態 API を呼び続けると CPU はアイドルにならない。

非対話環境で CLI の添付ヘルパーを使う場合は、この MCP 用に発行済みのアクセストークンを環境変数 `SPLIT_MCP_OAUTH_TOKEN` で渡せる。

HTTPS 接続を閉じるには、Worker の `SPLIT_REMOTE_MCP` を削除してから、専用の Access アプリを無効にする。ブラウザ用 Access アプリと stdio のトークンはそのまま使える。

## ローカル stdio：このマシンで一度だけ準備する

リポジトリのルートで実行する。Node **24.22.2**、MCP **0.52.206**、ComfyUI **v0.43.0**（frontend **1.57.4**）で固定している。初めて split を構築する場合は、先に [Access と Worker](#access-と-worker) を設定する。

```bash
mise install node@24.22.2
mise exec node@24.22.2 -- npm --prefix mcp ci --ignore-scripts
test -e .env.comfy-mcp || cp .env.comfy-mcp.example .env.comfy-mcp
# エディタで .env.comfy-mcp の URL・Client ID・Client Secret を記入する。
```

`COMFYUI_URL` は split の **Access 公開ホストの HTTPS オリジン**。Modal の直 URL や `/api` を付けた URL は使わない。すべてのクライアントが同じランチャーを起動し、ランチャーだけが専用ファイルから Access の認証情報を読み込む。クライアントを増やしてもトークンの再発行や再デプロイは不要。

## 好きなクライアントに登録する

**汎用の設定**: リポジトリのルートで以下を実行すると、このマシン用の `mcpServers` JSON が表示される。クライアントがこの形式に対応していれば、出力の `comfyui-split` エントリを既存の設定に追加する。設定画面を使う場合は、通信方式に **stdio**、名前に `comfyui-split`、コマンドと引数に出力の `command` と `args` を指定する。環境変数の入力は不要。パスは絶対パスなので、チェックアウトを動かしたときは再登録する。

```bash
mise exec node@24.22.2 -- node mcp/client-config.mjs
```

この stdio 構成はローカルプロセスを起動できるクライアント向け。リモートの MCP URL を使うクライアントには、ブラウザ用のルート URL ではなく同じホストの `/mcp` を登録する。

**Claude Code**: リポジトリのルートで一度だけ次を実行する。[`--scope local`](https://code.claude.com/docs/en/mcp#local-scope) はこのユーザーのこのプロジェクトだけに適用される。

```bash
claude mcp add --transport stdio --scope local comfyui-split -- \
  "$(mise exec node@24.22.2 -- node -p 'process.execPath')" "$PWD/mcp/start.mjs"
claude mcp get comfyui-split
```

`Connected` を確認したら、このディレクトリで `claude` を起動する。セッション内では `/mcp` で接続状態を確認できる。

**Codex**: 以下のコマンドでこのチェックアウトの設定に同じランチャーを登録し、新しいセッションを開く。

```bash
mise exec node@24.22.2 -- node mcp/register.mjs
codex mcp get comfyui-split
```

## Access と Worker

1. Zero Trust → Access controls → Applications で、既存アプリの公開ホストとポリシーを確認する。共有 AUD の全ホストがこの Worker の Custom Domains に対応すること、対象ホストの転送先が **既存の split CPU** であることを照合する。別の経路やワイルドカードを含むアプリに、このポリシーをそのまま追加しない。ブラウザ用 Allow は残す。
2. Node の版をリポジトリ直下の `.node-version` に合わせる。Worker で `npm ci`、`npm test`、`npm run typecheck` を実行する。本番のルート・バインディングを控え、[既存 Worker](cloudflare-access.md) に `npm run deploy` で転送制限を先に適用する。MCP 設定が空の間はサービスリクエストを拒否する。
3. Service credentials で有効期限を指定して専用トークンを発行する。この時点ではまだアプリに許可しない。Client ID（`.access` で終わる値。トークンの UUID とは異なる）と Client Secret は次節の専用ファイルへ保存する。
4. `worker/` で `npx --no-install wrangler secret put SPLIT_MCP_ACCESS`。対話入力する JSON の形式は次の通り。既存の origin マップを維持し、実際の値は Git に入れない。Worker に Client Secret は登録しない。

   ```json
   {"client_id":"<client-id>.access","hostname":"split.example.com","origin":"https://<workspace>--comfyui-split-ui.modal.run"}
   ```

5. **Service Auth** ポリシーを作り、Include → **Service Token** でこのトークンだけを選び、既存アプリに追加する。Any Access Service Token、Bypass は使わない。Worker は署名・issuer・audience・期限を検証した JWT の `common_name` を Client ID と照合し、ホストとその Modal オリジンが両方一致するときだけ中継する。HTTP と WebSocket とも、Modal へ送る前に Access の ID・Secret・JWT・認証 Cookie を除き、`X-Modal-Execution-Mode: split` を付ける。常時起動モードなら生成せず 409 になる。

## 切り戻し

特定のクライアントだけを外す場合は、そのクライアントの登録だけを削除する。Claude Code はこのプロジェクトで `claude mcp remove --scope local comfyui-split`、Codex はこのチェックアウトの `.codex/config.toml` から `[mcp_servers.comfyui-split]` を削除する。ほかのクライアントで使う間は、専用トークンと `.env.comfy-mcp` を残す。

すべてのクライアントの接続を終了するときは、Access で専用トークンを失効し、その Service Auth ポリシーだけをアプリから外す。Worker の `SPLIT_MCP_ACCESS` を削除して専用 JWT の転送も閉じる。既存のブラウザ用 Allow と Modal の Proxy Auth は維持する。元の Worker に戻す必要があれば、この順序で機械用の許可を外してから以前の Worker バージョンに戻す。ローカルの登録と専用の認証ファイルも削除する。ComfyUI 自体を戻すときは、`scripts/modal.sh app history comfyui-split` で更新前のバージョンを確認し、`scripts/modal.sh app rollback comfyui-split <バージョン>` で戻す。既存の Volume を削除する必要はない。
