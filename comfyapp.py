import subprocess
import shutil
from pathlib import Path

import modal

volume = modal.Volume.from_name("model-weights-vol", create_if_missing=True)
MODEL_VOLUME_DIR = Path("/models")
CHECKPOINT_DIR = Path("/root/.cache/comfyui/models/checkpoints")
MODEL_FILE = "flux1-schnell-fp8.safetensors"
MODEL_REPO = "Comfy-Org/flux1-schnell"

# 使用するカスタムノードのリスト
NODES = []

# イメージファイルの作成
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install("comfy-cli==1.5.1")
    .run_commands("comfy --skip-prompt install --nvidia")
    .run_commands(*[f"comfy node install {node}" for node in NODES])
)

app = modal.App(name="example-comfyui", image=image)


@app.function(
    allow_concurrent_inputs=10,
    concurrency_limit=1,
    container_idle_timeout=30,
    timeout=1800,
    gpu="T4",
    volumes={MODEL_VOLUME_DIR.as_posix(): volume},
)
@modal.web_server(8000, startup_timeout=60)
def ui():
    source_model = MODEL_VOLUME_DIR / MODEL_REPO / MODEL_FILE
    if not source_model.exists():
        raise FileNotFoundError(f"モデルファイルが Volume 内で見つかりません: {source_model}")

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    target_model = CHECKPOINT_DIR / MODEL_FILE
    if not target_model.exists():
        shutil.copy2(source_model, target_model)

    subprocess.Popen("comfy launch -- --listen 0.0.0.0 --port 8000", shell=True)
