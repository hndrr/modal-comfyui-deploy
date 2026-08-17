# modal-comfyui

Modal 上で ComfyUI を動かしつつ、Hugging Face のモデルを Modal Volume に保存して利用するためのリポジトリです。

![ComfyUI](assets/2025-09-28-21-11-34.png)

今の主要機能は次の 4 つです。

- `comfyapp.py`: ComfyUI 本体を Modal にデプロイする
- `preserve_model.py`: Hugging Face の単一ファイル保存と、その Web GUI を Modal にデプロイする
- `preserve_model_gui.py`: `preserve_model.py` を Gradio UI から呼び出す（ローカル実行）
- `web/`: ComfyUI のモデル・入力・出力を React + Hono 管理画面から操作する（`modal volume` CLI 経由）

補助スクリプトとして `rename_volume.py` と `move_volume_file.py` も含まれています。

## セットアップ

```bash
git clone https://github.com/hndrr/modal-comfyui.git
cd modal-comfyui
uv sync
```

事前に以下を済ませてください。

- Modal CLI でログインしておく
- Hugging Face からモデルを取得する場合は Modal Secret `huggingface-secret` を作成しておく
- ComfyUI 用の環境変数を使う場合は `.env.example` を `.env` にコピーして必要に応じて編集する

```bash
cp .env.example .env
```

`.env` は `comfyapp.py` 実行時に自動で読み込まれます。すでにシェルで設定済みの環境変数がある場合はそちらが優先されます。

Modal アカウントを複数使い分けている場合のみ、[docs/modal-profiles.md](docs/modal-profiles.md) を参照してください（1 アカウントなら不要です）。

## 1. ComfyUI を Modal で起動する

```bash
uv run modal serve comfyapp.py    # ローカル開発
uv run modal deploy comfyapp.py   # デプロイ
```

`comfyapp.py` は `modal.App(name="comfyui")` を定義し、ComfyUI を `8000` 番ポートの Web サーバーとして起動します。

GPU の切り替え、環境変数、永続化に使う Volume、custom nodes は [docs/comfyui-modal.md](docs/comfyui-modal.md) を参照してください。

## 2. Hugging Face のモデルを Volume に保存する

```bash
uv run modal run preserve_model.py::preserve_model \
  --repo-id "Comfy-Org/Qwen-Image_ComfyUI" \
  --filename "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors" \
  --destination-subdir "text_encoders"
```

![Modal/Storage](assets/2025-09-28-23-54-39.png)

Gradio GUI からも実行できます。

```bash
uv run preserve_model_gui.py      # http://127.0.0.1:7860
```

![Gradio](assets/2025-09-28-22-01-40.png)

保存先の決まり方、GUI の入力形式、Modal へのデプロイは [docs/preserve-model.md](docs/preserve-model.md) を参照してください。

## 3. ComfyUI 資産を管理する

`web/` の React + Hono アプリから、`comfy-model` / `comfy-inputs` / `comfy-outputs` をローカル管理画面で操作します。

```bash
cd web 
npm install
npm run build
npm start   # http://127.0.0.1:7860
```

<img width="1342" height="1050" alt="スクリーンショット 2026-08-04 20 47 32" src="https://github.com/user-attachments/assets/fc568c18-7f0e-4e2f-9f1f-7591fda7e3d7" />

機能と API は [docs/asset-manager.md](docs/asset-manager.md) を参照してください。

## 4. Volume を操作する補助スクリプト

Volume の別名コピーとファイル移動に `rename_volume.py` / `move_volume_file.py` を用意しています。使い方は [docs/volume-tools.md](docs/volume-tools.md) を参照してください。

## ファイル一覧

- `comfyapp.py`: ComfyUI の Modal デプロイ本体
- `preserve_model.py`: Hugging Face モデル保存処理と Modal 上の Web GUI
- `preserve_model_gui.py`: モデル保存 GUI（ローカル実行）
- `asset_manager.py`: Modal Volume 資産の CRUD サービス（Python / テスト・CLI 向け）
- `web/`: React GUI + Hono API（`modal volume` CLI 経由の資産管理画面）
- `asset_manager_gui.py`: 旧 Gradio 起動の廃止スタブ
- `rename_volume.py`: Volume コピー補助
- `move_volume_file.py`: Volume 内ファイル移動補助
- `main.py`: 最小のエントリーポイント
- `scripts/modal.sh`: Modal アカウントを複数使い分ける場合のみ使う CLI ラッパー（任意）
- `worker/`: Cloudflare Access 用のリバースプロキシ Worker（[docs/cloudflare-access.md](docs/cloudflare-access.md)）

## ドキュメント

使い方（`docs/`）:

- [comfyui-modal.md](docs/comfyui-modal.md): ComfyUI の GPU / 環境変数 / Volume / custom nodes
- [known-issues.md](docs/known-issues.md): 既知の不具合と注意点
- [preserve-model.md](docs/preserve-model.md): モデル保存の CLI・GUI・Modal デプロイ
- [asset-manager.md](docs/asset-manager.md): 資産管理画面（`web/`）
- [volume-tools.md](docs/volume-tools.md): Volume のコピーとファイル移動
- [modal-profiles.md](docs/modal-profiles.md): Modal アカウントを複数使い分ける場合の profile 運用（任意）
- [cloudflare-access.md](docs/cloudflare-access.md): Cloudflare Access + Worker で ComfyUI にログイン画面を付ける

設計・検討メモ（`docs/design/`）:

- [cloudflare-access.md](docs/design/cloudflare-access.md): Worker の責務と WebSocket 透過、迂回経路を塞ぐ設計
- [modal-idle-scale-to-zero.md](docs/design/modal-idle-scale-to-zero.md): アイドル時に GPU コンテナをゼロ台へ縮退させる設計
- [modal-power-control.md](docs/design/modal-power-control.md): ComfyUI から GPU の Sleep / Wake を操作する構想
- [pytorch-cu130-upgrade.md](docs/design/pytorch-cu130-upgrade.md): PyTorch / CUDA のアップグレード検討
