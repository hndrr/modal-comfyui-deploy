# MCP クライアントから Modal の ComfyUI split を使う

既存のローカル接続では、手元の MCP クライアントが [stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#stdio) で [artokun/comfyui-mcp](https://github.com/artokun/comfyui-mcp) を起動し、Cloudflare Access → Worker → split の CPU 入口に接続する。ノードの入力を調べる、ワークフローを編集・検証する、投入する、同じジョブを追跡する、画像を取得する操作が使える。生成を投入したときだけ split の GPU が必要になる。ブラウザ、Agent Panel、トンネルは不要。

[Comfy-Org/comfy-mcp](https://github.com/Comfy-Org/comfy-mcp) は Comfy 公式の別の MCP サーバー。ここでは既存の HTTPS 入口に Access のサービス認証を付けられる artokun 版を使う。リモート接続では同じ版を Modal の split CPU 上で起動する。

## リモート URL で使う

ChatGPT Web から画像・動画の生成を投入するには、書き込み操作を使える Business または Enterprise/Edu ワークスペースが必要です。Business で接続を作成できるのは管理者・オーナー、Enterprise/Edu では権限を付与されたメンバーも作成できます。`enqueue_workflow` の実行はワークスペースの操作権限にも従います。Pro のカスタム MCP 接続は読み取り・取得のみです。これは ChatGPT Web の条件で、Claude Code や Codex の接続条件とは別です。[OpenAI の接続条件](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)

以下の例では split ホストを `split.example.com` とする。本人用の接続 URL は `https://split.example.com/mcp`。ChatGPT Web は「プラグイン → 追加 → カスタム MCP サーバーを追加」で URL と OAuth を選び、本人の Access ログインで接続する。生成の書き込み操作が許可されていることを確認する。Claude Code と Codex も同じ URL を使える。ローカル stdio と併用する場合は別名で登録する。

```bash
claude mcp add --transport http --scope local comfyui-remote https://split.example.com/mcp
claude mcp login comfyui-remote
codex mcp add comfyui-remote --url https://split.example.com/mcp
codex mcp login comfyui-remote
```

ツール一覧・接続維持は Worker 内で処理する。ノード照会・履歴・画像取得で初めて CPU が起動し、生成の受付後に GPU が起動する。結果の画像は MCP の画像コンテンツで受け取る。サーバーの保存パスを指定する必要はない。

「EmptyImage と SaveImage の必須入力を調べて。生成はしないで」で照会を確認できる。生成時は `enqueue_workflow` に API 形式の `workflow` と新しい `request_id` を渡す。受付が不明なら**同じキーと同じワークフローでのみ再試行**する。キーを変えれば新しい生成になる。受付成功時の応答で返った `prompt_id` で `queue`・`get_history` を照会し、履歴のファイル名を `get_image` に渡す。チャットを閉じても受付済みの生成は続く。起動中は少し待って再試行する。初期化失敗や常時起動モードの競合は自動再試行しない。

### 動画を取り出す

生成の受付後に返った `prompt_id` を渡して `get_video` を呼ぶと、完了したジョブの MP4 を取得できる。例：「さっきの prompt_id の動画を get_video で返して」。再生成や Modal SDK は不要。1件のジョブに複数の MP4 があれば、`index`（0 始まり）で選ぶ。

4 MiB 以下は MCP の埋め込みリソース（`video/mp4`）としてバイト列を返す。MCP Apps 対応クライアント向けには再生画面も返す。埋め込みに対応しないクライアントでは、結果の URL をブラウザで開く（既存の split の Access ログインが必要）。4 MiB を超える動画はリンクで返す。MCP だけでファイルを保存する場合は `byte_offset: 0` で呼び、各応答の `resource.blob` を Base64 デコードして順番に連結する。`next_offset` が null になれば完了。`chunk_bytes` で小さめの応答も指定できる。ファイルパスや任意の URL を入力する機能はない。

### 音声を取り出す

「この `prompt_id` の音声を返して」で、エージェントが `get_audio` を呼ぶ。対象は完了したジョブが音声ファイルとして保存した FLAC・MP3・WAV・Ogg・Opus。複数あれば `index`（0 始まり）で選ぶ。4 MiB 以下は MCP の音声（`type: audio`）、それを超えるものは認証付きリンクで返す。分割取得は動画と同じく `byte_offset` と `next_offset` を使う。動画の中にしか音声がない場合は `get_video` で動画を取得するか、生成時に音声保存ノードも接続する。

### 保存済みワークフロー・素材入力・取消

- 「保存済みのワークフローを一覧にして」で `get_workflow(action: "list")` を使う。一覧に出た相対名（例：`動画/短いクリップ.json`）を `action: "get"` に渡す。既定の `format: "api"` は実行用、`format: "ui"` は保存した編集用グラフ。変換警告があれば確認してから、実行用グラフを既存の `enqueue_workflow` に渡す。取得だけでは生成しない。
- ChatGPT Web では「この添付を `upload_media` で保存して」と頼む。画像（PNG・JPEG・WebP）、音源（WAV・MP3・FLAC・M4A・Ogg/Opus）、動画（MP4・MOV・WebM・MKV）を各 256 MiB まで受け付ける。返った `filename` を LoadImage、LoadAudio、LoadVideo などの入力に使う。ChatGPT の設定に古いツール一覧が残っていれば、プラグインの「管理 → ツールを更新」で取り直す。
- CLI では次のヘルパーにファイルを渡す。初回のブラウザ認証には既存の本人用 OAuth を使う。1回のコマンドで複数の素材を送れる。ファイルパスはローカルでだけ読み、会話にファイルの Base64 を貼る必要はない。

```bash
node mcp/upload.mjs https://split.example.com/mcp photo.png sound.wav clip.mp4
```

ヘルパーは OAuth のログイン URL を端末に表示する。すでに同じ MCP 用に発行したアクセストークンを持つ非対話クライアントでは、環境変数 `SPLIT_MCP_OAUTH_TOKEN` も利用できる。アップロードに失敗したら同じファイルで再実行する。転送 ID とオフセットが一致すれば続きから送る。未完了のデータは入力候補に出ず、24時間後以降の新規転送時に整理する。保存済み素材はこの整理では削除しない。

- 「この `prompt_id` のジョブだけ取り消して」で `cancel_job` を使う。待機中なら `cancelled`、割り込みを送った場合は `cancel_requested` が返る。後者はまだ停止完了ではないので `queue` で確認する。完了済みのジョブの出力は残る。

### HTTPS MCP の各ツールはどこで動くか

| 操作 | 実装 |
| --- | --- |
| 初期化・ツール一覧 | Worker のみ。Modal は起動しない |
| `create_workflow`・`get_workflow`・`queue`・`get_history`・`get_image` | Worker → split CPU → 固定版 artokun の stdio サーバー。CPU は呼び出しごとにプロセスを起動する |
| `upload_media`・`cancel_job` | Worker → split CPU の入力 Volume またはジョブ台帳。新しい GPU は起動しない |
| `enqueue_workflow` | Worker → split の `/prompt`。受付を重複させないために `request_id` を使う。生成は GPU が実行する |
| `get_video`・`get_audio` | Worker → split CPU の `/history/<prompt_id>` と `/view`。既存の出力を読むだけで GPU は不要 |

`get_video` と `get_audio` はこのリポジトリの `worker/src/media.ts` にあり、artokun のコード、npm パッケージ、ローカル stdio サーバーは変更しない。artokun のバージョンを更新する際に動画・音声取得のパッチを移植する必要はない。

### リモート入口をデプロイする

1. split の CPU イメージに固定版 Node.js と MCP をデプロイする（`scripts/modal.sh deploy splitapp.py`）。GPU に Node/MCP は不要。既存 Worker をテスト後にデプロイする。一般の中継ルートでは内部呼び出し用ヘッダーを必ず取り除く。
2. split ホスト全体を従来のブラウザ・stdio 用 Access アプリに登録する。同じホストの `/mcp` に、より具体的なパスを持つ別の self-hosted Access アプリを設定し、Include が本人のメール1件のみの Allow を関連付ける。ブラウザ用共有アプリとは別の AUD を使う。Managed OAuth を有効にし、CLI のループバックと、ChatGPT が表示する**正確なコールバック URI**を許可する。トークンや本人メールは Git に記録しない。

   ChatGPT の登録フォームで動的クライアント登録（DCR）を選ぶと、コールバック URL が見えない場合がある。「OAuthの詳細設定 → 登録方法」で一度「ユーザー定義の OAuth クライアント」を選ぶと、このフォーム用の URL が表示される。それを Cloudflare の「許可されたリダイレクト URI」に追加して保存し、**同じフォームで DCR に戻して**プラグインを作成する。クライアント ID やシークレットの手入力、ワイルドカードの許可は不要。「接続」で Cloudflare に移ったら本人のログインを完了する。
3. Worker の secret `SPLIT_REMOTE_MCP` に次の JSON を対話入力する。既存の split ホストが解決する転送先と `origin` が一致しない限り中継されない。ホスト変更時は、既存の暗号化済みマップを置き換えずに `SPLIT_UI_ORIGIN` に `{"split.example.com":"https://example.modal.run"}` を設定できる。stdio の `SPLIT_MCP_ACCESS` とローカルの `COMFYUI_URL` も新ホストに合わせ、Worker の Custom Domain として登録する。

   ```json
   {"hostname":"split.example.com","split_hostname":"split.example.com","origin":"https://example.modal.run","audience":"<専用アプリのAUD>","owner_email":"owner@example.com"}
   ```

4. 認証なしでは OAuth のチャレンジとなること、別アプリの JWT では拒否されることを確認する。本人の OAuth で接続後、管理側から CPU/GPU の台数を読む。アプリの状態 API を定期照会すると CPU を起こすため、停止判定には使わない。
5. CPU/GPU がともに0台の状態から、ツール一覧で0/0、実ノード照会で1/0を確認する。再び0/0になるのを待って、下記の検証ワークフローを1回投入。同じキーの再送でジョブは1件だけ、64×64 PNG の回収と停止を確認する。停止後の再取得は CPU だけを起こす。停止待ち設定30秒は実際の停止時刻の保証ではない。
6. MCP だけを切り戻す場合は `SPLIT_REMOTE_MCP` を外し、`/mcp` が Worker で拒否されることを確認してからパス専用の Access アプリを無効化する。共有ホストの Custom Domain と `SPLIT_UI_ORIGIN` は残す。既存のブラウザ用 Allow、stdio 用の Service Auth、Modal の Proxy Auth は維持する。

**CPU 側の実測（2026-11-02）**: Modal の管理 API で CPU/GPU が 0/0 であることを確認後、デプロイした内部ツール経路から両ノードを照会した。準備完了まで63秒、照会後は1/0、アイドル後は0/0だった。もう一度0台から既存の64×64 PNGを取得し、起動中の応答を経てインライン画像が返った。これは本人用 OAuth を通す前の計測であり、ChatGPT からの生成結果とは区別する。

**同じホストの HTTPS MCP での実測（2026-11-02）**: ChatGPT Web で OAuth 接続後、CPU/GPU が0/0の状態でツールを更新し、ノード照会で1/0、アイドル後に0/0へ戻った。そこから ChatGPT で `EmptyImage → SaveImage` を投入すると1/1になり、ジョブ台帳に完了1件・出力1枚を記録した。受付済みの内容とキーを再送すると同じ `prompt_id` が返り、GPU の呼び出し ID と出力は同じだった。0/0に戻った後、Claude Code で PNG を再取得した。初回は起動中、再試行でインライン画像を取得し、保存された64×64 PNGとバイト単位で一致した。再取得時の台数は0/0 → 1/0 → 0/0。ChatGPT の画面では最終回答を表示できなかったため、ジョブ完了と画像は台帳と取得したファイルで確認した。

## ローカル stdio：このマシンで一度だけ準備する

リポジトリのルートで実行する。Node **24.22.2**、MCP **0.52.206**、ComfyUI **v0.43.0**（frontend **1.57.4**）で固定している。初めて split を構築する場合は、先に [Access と Worker](#access-と-worker) を設定する。

```bash
mise install node@24.22.2
mise exec node@24.22.2 -- npm --prefix mcp ci --ignore-scripts
test -e .env.comfy-mcp || install -m 600 .env.comfy-mcp.example .env.comfy-mcp
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

**最初の依頼例**（どのクライアントでも同じ）:

> comfyui-split で EmptyImage と SaveImage の必須入力を調べて。生成はしないで。

ツール呼び出しの承認が出たら、内容を確認して進める。生成したいときは「64×64 の青い画像を1枚だけ生成して、同じジョブの完了を待ち、PNG を手元に保存して」と依頼できる。複数のクライアントで同じジョブを見る場合は、受付後に返った `prompt_id` を渡す。

## 1回だけ生成して確認する

- **照会と GPU**: 他のジョブがない状態で、Modal の `gpu_worker` のコンテナ数を確認する。MCP クライアントから `create_workflow` の `action: node_info` に `node_type: EmptyImage`、次に `SaveImage` を指定する。照会後も GPU が0台であることを確認する。CPU のコールドスタートはあり得る。Worker のテストでは別ホストと誤った転送先を HTTP / WebSocket 両方で拒否する。
- **投入**: [検証ワークフロー](../mcp/smoke-workflow.json) を `enqueue_workflow` の `action: enqueue` に **1回だけ**渡し、返った `prompt_id` を控える。タイムアウトや再接続を理由に再投入しない。受付結果が不明なら元のキュー・履歴を調べる。
- **回収**: `queue` の `action: status` と `get_history` の `action: list` に同じ ID を渡す。成功時の履歴から SaveImage のファイル名とサブフォルダを取り、`get_image` の `action: get` に渡す。`save_dir` にこの checkout の `docs/validation/` の絶対パスを指定し、ローカルで PNG が開き、64×64であることを確認する。
- **終了**: ジョブの監視を終了し、他の生成・常時起動セッションがない状態で `gpu_worker` が0台に戻ることを確認する。Modal ダッシュボードまたは以下のメタデータ照会は GPU を起動しない。アプリの総タスク数には CPU も含まれるので区別する。

```bash
MODAL_PROFILE=<このcheckoutのプロファイル> uv run python -c \
  'import modal; print(modal.Function.from_name("comfyui-split","gpu_worker").get_current_stats().num_total_runners)'
```

2026年11月2日の実機検証では、Codex から両ノードを照会した前後の GPU は0台だった。上記ワークフローを1回投入し、返された同じ ID の成功履歴から 64×64 の PNG を取得してローカルで開いた。Modal の GPU 台数は0台 → 1台 → 0台。専用トークンを使った HTTP は split が200、共有アプリの他2ホストは403。WebSocket も split が101、別ホストが403だった。

汎用 MCP クライアントでも、上記の JSON から起動して両ノードの実際の入力定義を取得できた。Claude Code はローカル登録と `Connected` を確認した。

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
