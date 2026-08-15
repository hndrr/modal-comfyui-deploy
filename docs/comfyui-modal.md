# ComfyUI を Modal で起動する


ローカル開発:

```bash
uv run modal serve comfyapp.py
```

デプロイ:

```bash
uv run modal deploy comfyapp.py
```

`comfyapp.py` は `modal.App(name="comfyui")` を定義し、ComfyUI を `8000` 番ポートの Web サーバーとして起動します。

## 実装上のポイント

- Python 3.12 ベース
- PyTorch 2.10.0 + CUDA 13.0 系の wheel を利用
- `xformers`、`flash-attn`、`SageAttention` を組み込み
- `comfy-cli` で ComfyUI をインストール
- custom node を起動時イメージへ組み込み
- ComfyUI の `models` / `custom_nodes` / `output` / `input` / `user` を Modal Volume に接続
- ComfyUI の user data API を起動時に補正し、`user/workflows` 配下の workflow JSON を保存できるようにする

## 永続化に使う Volume

- `comfy-model`
- `comfy-custom-nodes`
- `comfy-outputs`
- `comfy-inputs`
- `comfy-user-data`

## GPU 切り替え

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

## 環境変数

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

## Cloudflare Access でログイン画面を付ける

ブラウザからそのまま使いたい場合は、Cloudflare Access を ComfyUI の手前に置き、Cloudflare Worker に `Modal-Key` / `Modal-Secret` を付与させる構成が使えます。ComfyUI 本体にも Modal にもログイン機能を足さずに、メールのワンタイム PIN や Google ログインで利用者を制限できます。

構成の説明とセットアップ手順は [docs/cloudflare-access.md](cloudflare-access.md) にまとめています。

## アイドル時のscale-to-zero

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

## 追加される custom nodes

- `https://github.com/crystian/ComfyUI-Crystools`
- `https://github.com/Firetheft/ComfyUI_Local_Media_Manager`
- `https://github.com/hayden-fr/ComfyUI-Image-Browsing`
- `https://github.com/rgthree/rgthree-comfy`

![ComfyUI](../assets/2025-09-28-21-11-34.png)
