# ComfyUI 資産を管理する


`web/` の React + Hono アプリが、次の Modal Volume をローカル管理画面から操作します。

- `comfy-model`
- `comfy-inputs`
- `comfy-outputs`

前提:

- Modal CLI でログイン済み（`uv run modal` または `modal` が使えること）
- Node.js 22+ 推奨

接続先は `.modal-profile` の固定先です（無ければ Modal CLI の既定）。画面ヘッダのバッジに接続先ワークスペース名が出るので、Volume が空に見えるときはまずそこを見てください。複数アカウントの扱いは [modal-profiles.md](modal-profiles.md) を参照してください。

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

管理画面はローカル利用前提です。更新系操作はサーバー内で直列化されます。

旧 Gradio の `asset_manager_gui.py` は廃止済みです（起動すると移行手順を表示して終了します）。
