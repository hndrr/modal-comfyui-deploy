# modal-comfyui

Modal 上で ComfyUI を動かしつつ、Hugging Face のモデルを Modal Volume に保存して利用するためのリポジトリです。

今の主要機能は次の 4 つです。

- `comfyapp.py`: ComfyUI 本体を Modal にデプロイする
- `preserve_model.py`: Hugging Face の単一ファイルを Modal Volume に保存する
- `preserve_model_gui.py`: `preserve_model.py` を Gradio UI から呼び出す
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

## 1. ComfyUI を Modal で起動する

ローカル開発:

```bash
uv run modal serve comfyapp.py
```

デプロイ:

```bash
uv run modal deploy comfyapp.py
```

`comfyapp.py` は `modal.App(name="comfyui")` を定義し、ComfyUI を `8000` 番ポートの Web サーバーとして起動します。

### 実装上のポイント

- Python 3.12 ベース
- PyTorch 2.10.0 + CUDA 13.0 系の wheel を利用
- `xformers`、`flash-attn`、`SageAttention` を組み込み
- `comfy-cli` で ComfyUI をインストール
- custom node を起動時イメージへ組み込み
- ComfyUI の `models` / `custom_nodes` / `output` / `input` / `user` を Modal Volume に接続
- ComfyUI の user data API を起動時に補正し、`user/workflows` 配下の workflow JSON を保存できるようにする

### 永続化に使う Volume

- `comfy-model`
- `comfy-custom-nodes`
- `comfy-outputs`
- `comfy-inputs`
- `comfy-user-data`

### GPU 切り替え

GPU はコード編集ではなく環境変数 `COMFYUI_GPU_PROFILE` で切り替えます。

利用可能な値:

- `rtx-pro-6000` 既定値
- `h100`
- `a100-80gb`

各プロファイルの対応:

- `rtx-pro-6000` -> Modal GPU `RTX-PRO-6000` / `TORCH_CUDA_ARCH_LIST=12.0+PTX`
- `h100` -> Modal GPU `H100` / `TORCH_CUDA_ARCH_LIST=9.0`
- `a100-80gb` -> Modal GPU `A100-80GB` / `TORCH_CUDA_ARCH_LIST=8.0`

例:

```bash
COMFYUI_GPU_PROFILE=h100 uv run modal serve comfyapp.py
```

### 環境変数

`.env.example`:

```env
COMFYUI_GPU_PROFILE=rtx-pro-6000
COMFYUI_SAGE_ATTENTION=on
COMFYUI_REQUIRES_PROXY_AUTH=off
COMFYUI_SCALEDOWN_WINDOW=30
COMFYUI_FUNCTION_TIMEOUT=1800
COMFYUI_CLI_ARGS=
COMFYUI_FORCE_BUILD=on
```

意味:

- `COMFYUI_GPU_PROFILE`: 使用する GPU プロファイル
- `COMFYUI_SAGE_ATTENTION`: `on` または `off`
- `COMFYUI_REQUIRES_PROXY_AUTH`: Modal proxy auth を要求する場合は `on`
- `COMFYUI_SCALEDOWN_WINDOW`: 接続終了後にコンテナを縮退するまでの最大秒数（`2`〜`1200`、既定値 `30`）
- `COMFYUI_FUNCTION_TIMEOUT`: 1入力または接続の最大実行秒数（`1`〜`86400`、既定値 `1800`）
- `COMFYUI_CLI_ARGS`: `comfy launch -- ...` の末尾に追加する引数
- `COMFYUI_FORCE_BUILD`: ComfyUIのインストール層以降を再ビルドする場合は `on`

`COMFYUI_SAGE_ATTENTION=on` が既定です。`COMFYUI_CLI_ARGS` に `--use-sage-attention` を自分で含めていない限り、自動で付与されます。

ComfyUI をその時点の最新版に更新してデプロイする場合:

```bash
COMFYUI_FORCE_BUILD=on uv run modal deploy comfyapp.py
```

この指定では CUDA、PyTorch、SageAttention のビルド層には既存キャッシュを利用し、ComfyUI のインストール層とそれ以降の層だけを再ビルドします。

Modal の公開 URL に `Modal-Key` / `Modal-Secret` ヘッダーを必須にする場合:

```bash
COMFYUI_REQUIRES_PROXY_AUTH=on uv run modal serve comfyapp.py
```

`COMFYUI_REQUIRES_PROXY_AUTH=off` が既定です。`on` にすると通常のブラウザアクセスでは開けないため、ヘッダーを付与できるクライアントやプロキシ経由で利用します。

### アイドル時のscale-to-zero

ComfyUI用Functionは `min_containers=0` のため、アクティブな入力がなければGPUコンテナをゼロ台まで縮退できます。`COMFYUI_SCALEDOWN_WINDOW` は、最後の入力が終了してから縮退するまでの最大アイドル時間です。短くするとアイドル中のコンピュート消費を抑えやすくなりますが、次回アクセス時のコールドスタートが増えます。

`COMFYUI_FUNCTION_TIMEOUT` はアイドル停止時間ではありません。生成処理を含む1入力やWebSocket接続を継続できる最大時間です。長時間の生成を行う場合は、想定する処理時間より長い値を指定してください。

例として、接続終了から10秒で縮退対象にし、Function timeoutを1時間にする場合:

```bash
COMFYUI_SCALEDOWN_WINDOW=10 \
COMFYUI_FUNCTION_TIMEOUT=3600 \
uv run modal deploy comfyapp.py
```

これらはデプロイ時に決まる設定なので、変更後は再デプロイが必要です。不正な整数や許容範囲外の値を指定すると、イメージビルド前にエラーになります。

ComfyUIはブラウザとの状態同期にWebSocketを使います。生成が終わっていてもComfyUIのタブやAPIクライアントが接続中だと、Modalではアクティブな入力として扱われ、scale-to-zeroしない可能性があります。確実に停止させる場合は、生成完了後にすべてのComfyUIタブとクライアントを閉じてください。再アクセス時は同じURLからコールドスタートします。

GPUコンテナがゼロ台になった後も、モデル、入力、出力、workflowなどを保持するModal Volumeのストレージは維持されます。

### 追加される custom nodes

- `https://github.com/crystian/ComfyUI-Crystools`
- `https://github.com/Firetheft/ComfyUI_Local_Media_Manager`
- `https://github.com/hayden-fr/ComfyUI-Image-Browsing`
- `https://github.com/rgthree/rgthree-comfy`

![ComfyUI](assets/2025-09-28-21-11-34.png)

## 2. Hugging Face のモデルを Volume に保存する

`preserve_model.py` は Hugging Face 上の単一ファイルをダウンロードし、ComfyUI が参照する `comfy-model` Volume に保存します。

```bash
uv run modal run preserve_model.py::preserve_model \
  --repo-id "Comfy-Org/Qwen-Image_ComfyUI" \
  --filename "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors" \
  --revision "main" \
  --destination-subdir "text_encoders"
```

### 保存先の決まり方

- `--destination-subdir` を指定した場合は、そのサブディレクトリ直下に保存
- 未指定の場合は `filename` のパス中から ComfyUI 向けサブディレクトリを自動判定
- 保存ファイル名は常に basename を使う

指定できる保存先:

- `audio_encoders`
- `checkpoints`
- `clip`
- `clip_vision`
- `controlnet`
- `detection`
- `diffusion_models`
- `embeddings`
- `latent_upscale_models`
- `loras`
- `text_encoders`
- `upscale_models`
- `vae`

注意点:

- `repo_id` と `filename` は必須
- Hugging Face へのアクセスには Modal Secret `huggingface-secret` が必要
- 既定のタイムアウトは 24 時間
- `max_containers=1` で同時実行を抑制

### デプロイ済み関数として使う

先にデプロイ:

```bash
uv run modal deploy preserve_model.py --name preserve-model
```

Python から呼ぶ例:

```bash
uv run python - <<'PY'
import modal

f = modal.Function.from_name("preserve-model", "preserve_model")
result = f.remote(
    repo_id="Comfy-Org/Qwen-Image-Edit_ComfyUI",
    filename="split_files/diffusion_models/qwen_image_edit_2509_bf16.safetensors",
    revision="main",
    destination_subdir="diffusion_models",
)
print(result)
PY
```

ログ確認:

```bash
uv run modal app logs preserve-model --tail
```

![Modal/Storage](assets/2025-09-28-23-54-39.png)

## 3. Gradio GUI からモデル保存する

`preserve_model_gui.py` は `preserve_model.py` を UI から実行するためのラッパーです。

ローカルの `app.run()` を使う既定モード:

```bash
uv run preserve_model_gui.py
```

デプロイ済み関数を使う:

```bash
uv run preserve_model_gui.py --use-deployed
```

既定のアクセス先:

- App 名: `preserve-model`
- Function 名: `preserve_model`

上書き方法:

- CLI 引数: `--deployed-app-name`, `--deployed-function-name`
- 環境変数: `PRESERVE_MODEL_DEPLOYED_APP`, `PRESERVE_MODEL_DEPLOYED_FUNCTION`
- デプロイ済み利用フラグ: `PRESERVE_MODEL_USE_DEPLOYED=1`

### GUI で受け付ける入力

1 つ目の入力欄には次のいずれかを入れられます。

- `repo_id::filename`
- `repo_id filename`
- Hugging Face の `resolve` / `blob` URL

例:

```text
Comfy-Org/Qwen-Image-Edit_ComfyUI::split_files/diffusion_models/model.safetensors
```

補足:

- リビジョン未指定時は `main`
- 保存先サブディレクトリは自動判定可能
- 自動判定できない場合はプルダウンで明示指定が必要
- 送信後は `FunctionCall` の完了を短時間だけ待ち、継続中なら確認手順を UI に表示
- UI から処理が中断された場合は `FunctionCall.cancel(terminate_containers=True)` を試行

主な起動オプション:

- `--use-deployed`
- `--use-local`
- `--share`
- `--server-port`
- `--server-name`

デフォルト URL:

`http://127.0.0.1:7860`

![Gradio](assets/2025-09-28-22-01-40.png)

## 4. ComfyUI 資産を管理する

`web/` の React + Hono アプリが、次の Modal Volume をローカル管理画面から操作します。

- `comfy-model`
- `comfy-inputs`
- `comfy-outputs`

前提:

- Modal CLI でログイン済み（`uv run modal` または `modal` が使えること）
- Node.js 22+ 推奨

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

## 5. Volume を別名へコピーする

`rename_volume.py` は Modal Volume 間でデータをコピーするユーティリティです。実質的に Volume 名を移行したい時に使います。

```bash
uv run python rename_volume.py <コピー元ボリューム名> <コピー先ボリューム名>
```

確認を省略する場合:

```bash
uv run python rename_volume.py <コピー元> <コピー先> --yes
```

仕様:

- コピー先 Volume は存在しなければ作成
- データコピーは `modal.App(name="volume-copier")` 経由で実行
- コピー後、元 Volume の削除は自動では行わない

## 6. Volume 内のファイルを移動する

`move_volume_file.py` は Modal Volume 内の単一ファイルまたはディレクトリを移動するユーティリティです。同じ Volume 内でのリネームにも、別 Volume への移動にも使えます。

```bash
uv run python move_volume_file.py \
  comfy-model \
  diffusion_models/old-model.safetensors \
  comfy-model \
  diffusion_models/archive/old-model.safetensors
```

別 Volume へ移動する例:

```bash
uv run python move_volume_file.py \
  comfy-inputs \
  uploads/example.png \
  comfy-outputs \
  archived/example.png
```

主なオプション:

- `--yes`: 確認プロンプトをスキップ
- `--overwrite`: 移動先が存在する場合に上書き
- `--create-destination-volume`: 移動先 Volume が存在しない場合に作成

注意点:

- パスは Volume 内の相対パスで指定する
- `..` や絶対パスは受け付けない
- 移動先に既存ファイルがある場合は `--overwrite` が必要
- 移動先パスが既存ディレクトリなら、その配下へ元ファイル名のまま移動する

## ファイル一覧

- `comfyapp.py`: ComfyUI の Modal デプロイ本体
- `preserve_model.py`: Hugging Face モデル保存処理
- `preserve_model_gui.py`: モデル保存 GUI
- `asset_manager.py`: Modal Volume 資産の CRUD サービス（Python / テスト・CLI 向け）
- `web/`: React GUI + Hono API（`modal volume` CLI 経由の資産管理画面）
- `asset_manager_gui.py`: 旧 Gradio 起動の廃止スタブ
- `rename_volume.py`: Volume コピー補助
- `move_volume_file.py`: Volume 内ファイル移動補助
- `main.py`: 最小のエントリーポイント
