import os
import subprocess
from pathlib import Path

import modal

volume = modal.Volume.from_name("model-weights-vol", create_if_missing=True)
MODEL_VOLUME_DIR = Path("/models")
MODEL_FILE = "flux1-schnell-fp8.safetensors"
MODEL_REPO = "Comfy-Org/flux1-schnell"
COMFY_ROOT_CANDIDATES = [
    Path("/root/comfy/ComfyUI"),
    Path("/root/ComfyUI"),
    Path("/root/.cache/comfyui/ComfyUI"),
]

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
    source_model_dir = MODEL_VOLUME_DIR / MODEL_REPO
    source_model = source_model_dir / MODEL_FILE
    if not source_model.exists():
        raise FileNotFoundError(f"モデルファイルが Volume 内で見つかりません: {source_model}")

    checkpoint_dirs = []
    for root_dir in COMFY_ROOT_CANDIDATES:
        models_dir = root_dir / "models"
        checkpoint_dir = models_dir / "checkpoints"
        if root_dir.exists():
            checkpoint_dirs.append(checkpoint_dir)

    if not checkpoint_dirs:
        # 最低でも先頭候補に配置する。存在しなければ後段の mkdir で生成する。
        checkpoint_dirs.append(COMFY_ROOT_CANDIDATES[0] / "models" / "checkpoints")

    for checkpoint_dir in checkpoint_dirs:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        target_link = checkpoint_dir / MODEL_FILE

        if target_link.exists() or target_link.is_symlink():
            if target_link.is_symlink():
                current_target = Path(os.readlink(target_link))
                if current_target != source_model:
                    target_link.unlink()
                    target_link.symlink_to(source_model)
            else:
                print(
                    f"警告: {checkpoint_dir} に既存ファイルがありリンクを作成できません"
                )
        else:
            target_link.symlink_to(source_model)

        print(f"{checkpoint_dir} にリンクを用意しました")

    subprocess.Popen("comfy launch -- --listen 0.0.0.0 --port 8000", shell=True)
