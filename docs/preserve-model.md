# Hugging Face のモデルを Volume に保存する


`preserve_model.py` は Hugging Face 上の単一ファイルをダウンロードし、ComfyUI が参照する `comfy-model` Volume に保存します。

```bash
uv run modal run preserve_model.py::preserve_model \
  --repo-id "Comfy-Org/Qwen-Image_ComfyUI" \
  --filename "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors" \
  --revision "main" \
  --destination-subdir "text_encoders"
```

アカウントを複数使い分けている場合は `./scripts/modal.sh run ...` を使います（[docs/modal-profiles.md](modal-profiles.md)）。

## 保存先の決まり方

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

## デプロイ済み関数として使う

先にデプロイ:

```bash
uv run modal deploy preserve_model.py --name preserve-model
```

このコマンドではモデル保存用の `preserve_model` と GUI 用の `web` が常にまとめてデプロイされます。

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

![Modal/Storage](../assets/2025-09-28-23-54-39.png)

## Gradio GUI からモデル保存する

`preserve_model_gui.py` は `preserve_model.py` を UI から実行するためのラッパーです。

```bash
uv run preserve_model_gui.py
```

実行のたびに `modal.App.run()` で一時コンテナを起動するため、**事前のデプロイは不要**です。既定 URL は `http://127.0.0.1:7860`。

接続先は `.modal-profile` の固定先です（[docs/modal-profiles.md](modal-profiles.md)）。

ブラウザから使いたい場合は、同じ UI を Modal にデプロイできます（後述）。

## GUI で受け付ける入力

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
- 送信後は完了まで UI に進捗（ダウンロード済みバイト数と経過時間）が流れる
- UI から処理が中断された場合は `FunctionCall.cancel(terminate_containers=True)` を試行

主な起動オプション:

- `--share`
- `--server-port`
- `--server-name`

## Modal にデプロイしてブラウザから使う

`preserve_model.py` は同じ GUI を Modal 上の Web アプリとしても公開します。UI の組み立ては `preserve_model_gui.py` を再利用しており、ローカル実行の手順は変わりません。

```bash
PRESERVE_WEB_REQUIRES_PROXY_AUTH=on uv run modal deploy preserve_model.py
```

複数アカウント時は `./scripts/modal.sh deploy preserve_model.py`。デプロイ先に Secret `huggingface-secret` が無いと失敗します。

Modal 上の App は `preserve-model` の 1 つで、そこに Function が 2 つ並びます。

- `preserve_model`: ダウンロードして Volume に保存する処理
- `web`: この GUI

両方の Function は同じファイルと App に定義されているため、どちらか一方だけが消えることはありません。

環境変数:

- `PRESERVE_WEB_REQUIRES_PROXY_AUTH`: Modal proxy auth を要求する場合は `on`（既定 `on`）。公開 GUI にする場合のみ明示的に `off`
- `PRESERVE_WEB_SCALEDOWN_WINDOW`: 縮退までの最大秒数（`2`〜`1200`、既定 `30`）
- `PRESERVE_WEB_FUNCTION_TIMEOUT`: 1入力の最大実行秒数（`1`〜`86400`、既定 `1800`）

コンテナ内では次の点がローカル実行と異なります。

- `modal.App.run()` による一時コンテナ起動は Modal が禁じているため、**デプロイ済み関数を呼ぶモードに固定**されます
- Modal の認証はコンテナ ID で自動的に通るため、`modal token` でのログインは不要です

`PRESERVE_WEB_REQUIRES_PROXY_AUTH=on` にすると Modal の直 URL は 401 になるので、ブラウザから使うには Cloudflare Access 越しに公開します。手順は [docs/cloudflare-access.md](cloudflare-access.md) を参照してください。1 つの Worker で ComfyUI と併せて出せます（`MODAL_ORIGINS` にホスト名を足すだけで、Worker のコード変更は不要）。

デフォルト URL:

`http://127.0.0.1:7860`

![Gradio](../assets/2025-09-28-22-01-40.png)
