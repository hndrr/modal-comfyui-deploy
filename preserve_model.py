from pathlib import Path
from typing import Optional

from datetime import datetime, timezone
import shutil

import modal

# create a Volume, or retrieve it if     it exists
volume = modal.Volume.from_name("comfy-model", create_if_missing=True)
MODEL_DIR = Path("/models")
COMFY_MODEL_SUBDIRS = {
    "checkpoints",
    "diffusion_models",
    "loras",
    "text_encoders",
    "audio_encoders",
    "clip",
    "clip_vision",
    "controlnet",
    "vae",
    "embeddings",
    "upscale_models",
}

# define dependencies for downloading model
download_image = (
    modal.Image.debian_slim()
    .pip_install("huggingface_hub[hf_transfer]")  # install fast Rust download client
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})  # and enable it
)
app = modal.App()


@app.function(
    volumes={MODEL_DIR.as_posix(): volume},  # Volume をマウントして関数と共有する
    image=download_image,
    timeout=60 * 60 * 24,  # 24時間に延長して大容量ダウンロードを許容
    concurrency_limit=1,  # 同時実行を制限してI/O競合を避ける
)
def preserve_model(
    repo_id: Optional[str] = None,
    filename: Optional[str] = None,
    revision: Optional[str] = None,
    destination_subdir: Optional[str] = None,
):
    from huggingface_hub import hf_hub_download

    def _resolve_destination(filename: str, destination_subdir: Optional[str]) -> Path:
        """保存先のフルパスを決定する。ルート直下にファイルを配置する"""

        filename_path = Path(filename)

        if destination_subdir is not None:
            if destination_subdir not in COMFY_MODEL_SUBDIRS:
                raise ValueError(
                    f"指定できる保存先は {sorted(COMFY_MODEL_SUBDIRS)} のいずれかです"
                )
            target_root = MODEL_DIR / destination_subdir
            target_root.mkdir(parents=True, exist_ok=True)
            return target_root / filename_path.name

        matched = next(
            (part for part in filename_path.parts if part in COMFY_MODEL_SUBDIRS),
            None,
        )
        if matched is None:
            raise ValueError(
                "ファイル名からComfyUIの保存先ディレクトリを特定できませんでした。"
            )
        target_root = MODEL_DIR / matched
        target_root.mkdir(parents=True, exist_ok=True)
        return target_root / filename_path.name

    if not repo_id:
        raise ValueError("repo_id を必ず指定してください")
    if not filename:
        raise ValueError("filename を必ず指定してください")

    filename_path = Path(filename)
    destination_path = _resolve_destination(filename, destination_subdir)
    downloaded_path = Path(
        hf_hub_download(
            repo_id=repo_id,
            filename=filename_path.as_posix(),
            revision=revision,
            local_dir_use_symlinks=False,
            resume_download=True,
        )
    )
    if downloaded_path.resolve() != destination_path.resolve():
        shutil.copy2(downloaded_path, destination_path)
        downloaded_path = destination_path
    file_stat = downloaded_path.stat()
    completed_at = datetime.now(timezone.utc).isoformat()
    print(f"モデルファイルを {downloaded_path} に保存しました")
    return {
        "destination": destination_path.as_posix(),
        "size_bytes": file_stat.st_size,
        "completed_at": completed_at,
    }
