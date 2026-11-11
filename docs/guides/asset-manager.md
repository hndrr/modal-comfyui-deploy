# ComfyUI 資産を管理する

split ホストにブラウザでログインし、`/assets` を開くと入力・出力・モデルを管理できます。`/models` は Hugging Face からのモデル保存画面です。どちらも本人の Access ログインが必要です。操作するのは split と同じ環境の Volume です。GUI の閲覧は CPU だけを使い、split の ComfyUI や GPU は起動しません。

**ホストへの設定**：既存の split と同じ Modal プロファイルを使い、以下を実行します。`<SPLIT_PUBLIC_ORIGIN>` は、Cloudflare Worker の Custom Domain に登録した split の公開ホスト名に `https://` を付けた URL です。パスや末尾の `/` は付けません。

```sh
SPLIT_GUI_PUBLIC_ORIGIN="<SPLIT_PUBLIC_ORIGIN>" scripts/modal.sh deploy -m comfy_split.split_guis
```

両方の Web 関数は Modal Proxy Auth が必須で、アイドル時の最小台数は 0 です。保存ジョブには既存の `huggingface-secret` が必要です。Worker の `SPLIT_ADMIN_GUIS` Secret に JSON で `hostname`（既存の `SPLIT_UI_ORIGIN` のホスト）、`owner_email`（許可する本人）、`models_origin`・`assets_origin`（各 web 関数の HTTPS origin）、`key`・`secret`（この Modal 環境の Proxy Auth）を登録して Worker をデプロイします。実際の値は Git に置きません。

再確認は、本人のブラウザで保存フォームと資産一覧を開き、MCP のサービス認証では `/assets/_ui/api/volumes` が拒否されることを見ます。`/models` の JSON API、`/models/<folder>`、ComfyUI の `/assets/<bundle>` は従来どおりです。GUI だけを取り下げるには Worker の `SPLIT_ADMIN_GUIS` を削除し、GUI アプリを停止します。

split の `/assets` でアップロードすると、管理画面に届く前に [Cloudflare のリクエストサイズ上限](https://developers.cloudflare.com/workers/platform/limits/#request-limits)が適用されます。Free・Pro ドメインでは、添付ファイルと multipart の付加情報を含めて **1リクエスト100 MB** までです。ドメイン側の上限を超えると HTTP 413 になり、管理画面の設定を引き上げても通りません。より大きなファイルは、次のローカル画面から同じ Volume にアップロードできます。

## 手元で起動する

`web/` の React + Hono アプリは、ローカルでも同じ 3 つの Volume を操作できます。

- `comfy-model`
- `comfy-inputs`
- `comfy-outputs`

前提:

- Modal CLI でログイン済み（`uv run modal` または `modal` が使えること）
- Node.js 24.22.2（リポジトリの `.node-version` に合わせる）

接続先は Modal CLI の既定アカウントです。画面ヘッダのバッジに接続先ワークスペース名が出るので、Volume が空に見えるときはまずそこを見てください。複数アカウントを使い分けている場合は [modal-profiles.md](../operations/modal-profiles.md) を参照してください。

初回ビルドと起動:

```bash
cd web
npm install
npm run build
npm start
```

既定 URL:

`http://127.0.0.1:7860`

開発時（Vite が UI、Hono が API）:

```bash
cd web
npm run dev
```

- UI: `http://127.0.0.1:5173`（`/api` は Hono `:7860` へ proxy）
- API: `http://127.0.0.1:7860`

ポート変更:

```bash
PORT=7861 npm start
```

<img width="1342" height="1050" alt="スクリーンショット 2026-08-04 20 47 32" src="https://github.com/user-attachments/assets/fc568c18-7f0e-4e2f-9f1f-7591fda7e3d7" />


管理画面はローカル利用専用です。`HOST` は `127.0.0.1`、`localhost`、`::1` などのloopbackアドレスだけを受け付けます。`0.0.0.0`、LANアドレス、公開アドレスを指定するとサーバーは起動を拒否します。リバースプロキシやトンネルを使った外部公開もサポートしません。

アップロード上限は `ASSET_UPLOAD_MAX_FILE_BYTES`（1ファイル、既定10 GiB）と `ASSET_UPLOAD_MAX_TOTAL_BYTES`（multipartリクエスト全体、既定20 GiB）で変更できます。どちらも正のバイト数で指定します。

構成:

- **GUI**: React + React Aria Components + Tailwind
- **API**: Hono（Node）
- **Volume I/O**: 常駐 Python ワーカー（`asset_rpc.py` + `asset_manager.py` / Modal SDK）  
  list はメタデータのみ。一覧はプロセス内キャッシュ。サムネ/本文は on-demand + ディスクキャッシュ。  
  （`modal volume` CLI は使わない。CLI 毎回起動だと Gradio より遅くなるため）

管理画面では次の操作ができます。

- Volume タブとフォルダを切り替えて、名前・サイズ・更新日時を確認
- **複数選択（チェック / Shift+クリック範囲選択）からの一括完全削除**（大量整理向け）
- `comfy-inputs` / `comfy-outputs` の画像ギャラリー（遅延ロード）
- 画像・動画・音声のプレビューとファイルのダウンロード
- ローカルファイルの複数アップロード
- 同一 Volume 内での名前変更・移動（1件）
- `comfy-inputs` と `comfy-outputs` の間での移動
- ファイル・フォルダの完全削除

Hugging Face からのモデル取り込みは別途 `preserve_model_gui.py`（Gradio）を使います。

Models へのアップロードと移動は、ComfyUI が認識するモデル種別ディレクトリ配下だけに制限されます。上書きは既定で無効です。

> [!WARNING]
> 削除はゴミ箱を経由しない完全削除です。確認ダイアログで「完全に削除する」を押した場合だけ実行されます。Volume ルート自体は削除できません。

ローカル起動時は手元で利用してください。split から公開する場合は、冒頭の本人用 Access 認証を設定します。更新系操作はサーバー内で直列化されます。
