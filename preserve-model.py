from typing import Optional
from pathlib import Path

import modal

# create a Volume, or retrieve it if it exists
volume = modal.Volume.from_name("model-weights-vol", create_if_missing=True)
MODEL_DIR = Path("/models")

# define dependencies for downloading model
download_image = (
    modal.Image.debian_slim()
    .pip_install("huggingface_hub[hf_transfer]")  # install fast Rust download client
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})  # and enable it
)
app = modal.App()

# https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI/resolve/main/split_files/diffusion_models/qwen_image_edit_2509_bf16.safetensors


@app.function(
    volumes={MODEL_DIR.as_posix(): volume},  # Volume をマウントして関数と共有する
    image=download_image,
)
def preserve_model(
    repo_id: str = "Comfy-Org/Qwen-Image-Edit_ComfyUI",
    filename: str = "split_files/diffusion_models/qwen_image_edit_2509_bf16.safetensors",
    revision: Optional[str] = None,
):
    from huggingface_hub import hf_hub_download

    target_dir = MODEL_DIR / repo_id
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded_path = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        revision=revision,
        local_dir=target_dir,
        local_dir_use_symlinks=False,
    )
    print(f"モデルファイルを {downloaded_path} に取得しました")
