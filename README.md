# Comfy UI

## clone

```bash
git clone https://github.com/hndrr/modal-comfyui.git
cd modal-comfyui
uv sync
```

## launch

```bash
uv run modal serve comfyapp.py
```

### GPU とコンテナ設定

- `comfyapp.py` 内の `@app.function` で `gpu="T4"` など Modal が提供する GPU 名を指定します。CPU のみで動かす場合は `gpu=None` にします。citeturn1search5
- 2025年9月時点で指定できる主な GPU 名は `T4`、`L4`、`A10`、`A100`、`A100-40GB`、`A100-80GB`、`L40S`、`H100`、`H200`、`B200` です。需要に合わせて `gpu="A100-80GB"` などと書き換えてください。citeturn1search5
- 複数 GPU を 1 コンテナに割り当てたい場合は `gpu="H100:4"` のように末尾へ `:台数` を付与します。B200/H200/H100/A100/L40S/L4/T4 は最大 8 台、A10 は最大 4 台まで指定できます。citeturn1search5
- GPU を複数候補で指定して可用性を高めたい場合は `gpu=["H100", "A100-40GB:2"]` のようにリストで渡すと優先順位付きフォールバックが機能します。citeturn0search0
- 料金は GPU 種類ごとに異なるため、[Modal の料金ページ](https://modal.com/pricing)で最新の秒課金単価を確認してください。例: `H100` は 1 秒あたり約 0.001097 ドル。citeturn1search0
- 同時実行数を増やしたい場合は `max_containers` を調整します。値を大きくすると並列に立ち上がる GPU コンテナが増え、利用料金も比例して増えます。
- 長時間の推論が必要な場合は `timeout` や `scaledown_window` を大きめに設定し、セッションが途中で停止しないようにします。
- 常時稼働させる際は `modal deploy comfyapp.py` を利用して常駐サービスとして公開できます。

![ComfyUI](assets/2025-09-28-21-11-34.png)

## model upload

```bash
uv run modal run preserve-model.py::preserve_model \
  --repo-id "Comfy-Org/Qwen-Image_ComfyUI" \
  --filename "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors" \
  --revision "main" \
  --destination-subdir "text_encoders"
```

### Gradio UI

```bash
uv run preserve_model_gui.py
```
