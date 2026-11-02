# Codex から Modal の ComfyUI split を使う

手元の Codex が stdio で [artokun/comfyui-mcp](https://github.com/artokun/comfyui-mcp) を起動し、Cloudflare Access → Worker → split の CPU 入口に接続する。ノードの入力を調べる、ワークフローを編集・検証する、投入する、同じジョブを追跡する、画像を取得する操作が使える。生成を投入したときだけ split の GPU が必要になる。ブラウザ、Agent Panel、トンネルは不要。

[Comfy-Org/comfy-mcp](https://github.com/Comfy-Org/comfy-mcp) は Comfy 公式の別の MCP サーバー。ここでは既存の HTTPS 入口に Access のサービス認証を付けられる artokun 版を使う。Modal に MCP サーバーをインストールする構成ではない。

## Access と Worker

1. Zero Trust → Access controls → Applications で、既存アプリの公開ホストとポリシーを確認する。共有 AUD の全ホストがこの Worker の Custom Domains に対応すること、対象ホストの転送先が **既存の split CPU** であることを照合する。別の経路やワイルドカードを含むアプリに、このポリシーをそのまま追加しない。ブラウザ用 Allow は残す。
2. Node の版をリポジトリ直下の `.node-version` に合わせる。Worker で `npm ci`、`npm test`、`npm run typecheck` を実行する。本番のルート・バインディングを控え、[既存 Worker](cloudflare-access.md) に `npm run deploy` で転送制限を先に適用する。MCP 設定が空の間はサービスリクエストを拒否する。
3. Service credentials で有効期限を指定して専用トークンを発行する。この時点ではまだアプリに許可しない。Client ID（`.access` で終わる値。トークンの UUID とは異なる）と Client Secret は次節の専用ファイルへ保存する。
4. `worker/` で `npx --no-install wrangler secret put SPLIT_MCP_ACCESS`。対話入力する JSON の形式は次の通り。既存の origin マップを維持し、実際の値は Git に入れない。Worker に Client Secret は登録しない。

   ```json
   {"client_id":"<client-id>.access","hostname":"split.example.com","origin":"https://<workspace>--comfyui-split-ui.modal.run"}
   ```

5. **Service Auth** ポリシーを作り、Include → **Service Token** でこのトークンだけを選び、既存アプリに追加する。Any Access Service Token、Bypass は使わない。Worker は署名・issuer・audience・期限を検証した JWT の `common_name` を Client ID と照合し、ホストとその Modal オリジンが両方一致するときだけ中継する。HTTP と WebSocket とも、Modal へ送る前に Access の ID・Secret・JWT・認証 Cookie を除き、`X-Modal-Execution-Mode: split` を付ける。常時起動モードなら生成せず 409 になる。

## ローカルの登録

リポジトリのルートで実行する。Node **24.22.2**、MCP **0.52.206**、ComfyUI **v0.43.0**（frontend **1.57.4**）で固定している。

```bash
mise install node@24.22.2
mise exec node@24.22.2 -- npm --prefix mcp ci --ignore-scripts
test -e .env.comfy-mcp || install -m 600 .env.comfy-mcp.example .env.comfy-mcp
# エディタで .env.comfy-mcp の URL・Client ID・Client Secret を記入する。
mise exec node@24.22.2 -- node mcp/register.mjs
codex mcp get comfyui-split
```

`COMFYUI_URL` は split の **Access 公開ホストの HTTPS オリジン**。Modal 直 URL や `/api` を付けた URL は使わない。ランチャーはプロジェクト専用の認証ファイルを使い、別オリジンへの HTTP リクエストとリダイレクトを拒否する。Codex の設定には起動コマンド、許可ツールとタイムアウトを保存し、認証情報は含めない。登録後、このプロジェクトで新しい Codex セッションを開く。対話画面では必要な MCP 呼び出しを承認する。非対話の `codex exec` で「approval policy is never」と出る場合は、`codex exec --approve-for-me` で個別の承認レビューを有効にできる。

## 1回だけ生成して確認する

- **照会と GPU**: 他のジョブがない状態で、Modal の `gpu_worker` のコンテナ数を確認する。Codex で `create_workflow` の `action: node_info` に `node_type: EmptyImage`、次に `SaveImage` を指定する。照会後も GPU が0台であることを確認する。CPU のコールドスタートはあり得る。Worker のテストでは別ホストと誤った転送先を HTTP / WebSocket 両方で拒否する。
- **投入**: [検証ワークフロー](../mcp/smoke-workflow.json) を `enqueue_workflow` の `action: enqueue` に **1回だけ**渡し、返った `prompt_id` を控える。タイムアウトや再接続を理由に再投入しない。受付結果が不明なら元のキュー・履歴を調べる。
- **回収**: `queue` の `action: status` と `get_history` の `action: list` に同じ ID を渡す。成功時の履歴から SaveImage のファイル名とサブフォルダを取り、`get_image` の `action: get` に渡す。`save_dir` にこの checkout の `docs/validation/` の絶対パスを指定し、ローカルで PNG が開き、64×64であることを確認する。
- **終了**: ジョブの監視を終了し、他の生成・常時起動セッションがない状態で `gpu_worker` が0台に戻ることを確認する。Modal ダッシュボードまたは以下のメタデータ照会は GPU を起動しない。アプリの総タスク数には CPU も含まれるので区別する。

```bash
MODAL_PROFILE=<このcheckoutのプロファイル> uv run python -c \
  'import modal; print(modal.Function.from_name("comfyui-split","gpu_worker").get_current_stats().num_total_runners)'
```

2026年11月2日の実機検証では、Codex から両ノードを照会した前後の GPU は0台だった。上記ワークフローを1回投入し、返された同じ ID の成功履歴から 64×64 の PNG を取得してローカルで開いた。Modal の GPU 台数は0台 → 1台 → 0台。専用トークンを使った HTTP は split が200、共有アプリの他2ホストは403。WebSocket も split が101、別ホストが403だった。

## 切り戻し

Access で専用トークンを失効し、その Service Auth ポリシーだけをアプリから外す。Worker の `SPLIT_MCP_ACCESS` を削除して専用 JWT の転送も閉じる。既存のブラウザ用 Allow と Modal の Proxy Auth は維持する。元の Worker に戻す必要があれば、この順序で機械用の許可を外してから以前の Worker バージョンに戻す。Codex はこの checkout の `.codex/config.toml` から `[mcp_servers.comfyui-split]` だけを取り除き、認証ファイルを削除してセッションを再起動する。ComfyUI 自体を戻すときは、`scripts/modal.sh app history comfyui-split` で更新前のバージョンを確認し、`scripts/modal.sh app rollback comfyui-split <バージョン>` で戻す。既存の Volume を削除する必要はない。
