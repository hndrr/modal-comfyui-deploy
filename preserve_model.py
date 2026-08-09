"""Hugging Face モデルの保存処理と、その Gradio Web UI の Modal App。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from typing import Final, Optional

import modal

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - Modal コンテナ内のみ通る経路
    # `.env` を読むのはデプロイを実行するローカル側だけ。
    # 解決済みの設定値は
    # Web UI イメージの環境変数として渡すため、コンテナでは何もしない。
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

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
    "latent_upscale_models",
    "upscale_models",
    "detection"
}

# define dependencies for downloading model
download_image = (
    modal.Image.debian_slim()
    .pip_install("huggingface_hub[hf_transfer]")  # install fast Rust download client
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})  # and enable it
)
app = modal.App("preserve-model")


@app.function(
    volumes={MODEL_DIR.as_posix(): volume},  # Volume をマウントして関数と共有する
    image=download_image,
    timeout=60 * 60 * 24,  # 24時間に延長して大容量ダウンロードを許容
    max_containers=1,  # 同時実行を制限してI/O競合を避ける
    secrets=[modal.Secret.from_name("huggingface-secret")],
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


# Gradio Web UI -------------------------------------------------------------

# preserve_model_gui.py を Web UI イメージ内で配置するディレクトリ。
# GUI は隣にある preserve_model.py の関数と定数を読み込む。
REMOTE_SOURCE_DIR: Final = "/root"

GRADIO_VERSION: Final = "5.47.2"
MODAL_VERSION: Final = "1.1.4"

PRESERVE_WEB_REQUIRES_PROXY_AUTH_ENV = "PRESERVE_WEB_REQUIRES_PROXY_AUTH"
PRESERVE_WEB_SCALEDOWN_WINDOW_ENV = "PRESERVE_WEB_SCALEDOWN_WINDOW"
PRESERVE_WEB_FUNCTION_TIMEOUT_ENV = "PRESERVE_WEB_FUNCTION_TIMEOUT"

DEFAULT_SCALEDOWN_WINDOW: Final = 30
MIN_SCALEDOWN_WINDOW: Final = 2
MAX_SCALEDOWN_WINDOW: Final = 1200

DEFAULT_FUNCTION_TIMEOUT: Final = 1800
MIN_FUNCTION_TIMEOUT: Final = 1
MAX_FUNCTION_TIMEOUT: Final = 86400

# preserve_model 側が max_containers=1 で I/O 競合を避けているので
# Web 側も揃える。
MAX_CONTAINERS: Final = 1
MIN_CONTAINERS: Final = 0

DOTENV_PATH = Path(__file__).with_name(".env")
load_dotenv(DOTENV_PATH, override=False)


def _resolve_on_off_env(env_name: str, default: str = "on") -> bool:
    raw = os.environ.get(env_name, default).strip().lower()
    if raw == "on":
        return True
    if raw == "off":
        return False
    raise ValueError(f"Invalid {env_name}: {raw!r}. Allowed values: on, off")


def _resolve_int_env(
    env_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(env_name)
    if raw is None:
        return default

    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"Invalid {env_name}: {raw!r}. "
            f"Expected an integer between {minimum} and {maximum}."
        ) from exc

    if not minimum <= value <= maximum:
        raise ValueError(
            f"Invalid {env_name}: {raw!r}. "
            f"Expected an integer between {minimum} and {maximum}."
        )

    return value


# 設定が不正ならイメージのビルド前にここで落とす。
REQUIRES_PROXY_AUTH = _resolve_on_off_env(PRESERVE_WEB_REQUIRES_PROXY_AUTH_ENV)
SCALEDOWN_WINDOW = _resolve_int_env(
    PRESERVE_WEB_SCALEDOWN_WINDOW_ENV,
    DEFAULT_SCALEDOWN_WINDOW,
    MIN_SCALEDOWN_WINDOW,
    MAX_SCALEDOWN_WINDOW,
)
FUNCTION_TIMEOUT = _resolve_int_env(
    PRESERVE_WEB_FUNCTION_TIMEOUT_ENV,
    DEFAULT_FUNCTION_TIMEOUT,
    MIN_FUNCTION_TIMEOUT,
    MAX_FUNCTION_TIMEOUT,
)

# GUI 自体には GPU も Volume も不要。モデル保存は同じ App の
# preserve_model 関数へ委譲する。
web_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(f"gradio=={GRADIO_VERSION}", f"modal=={MODAL_VERSION}")
    # コンテナ内で再 import されてもデプロイ時と同じ設定にする。
    .env(
        {
            PRESERVE_WEB_REQUIRES_PROXY_AUTH_ENV: (
                "on" if REQUIRES_PROXY_AUTH else "off"
            ),
            PRESERVE_WEB_SCALEDOWN_WINDOW_ENV: str(SCALEDOWN_WINDOW),
            PRESERVE_WEB_FUNCTION_TIMEOUT_ENV: str(FUNCTION_TIMEOUT),
        }
    )
    .add_local_file(
        Path(__file__).as_posix(),
        f"{REMOTE_SOURCE_DIR}/preserve_model.py",
    )
    .add_local_file(
        Path(__file__).with_name("preserve_model_gui.py").as_posix(),
        f"{REMOTE_SOURCE_DIR}/preserve_model_gui.py",
    )
)


@app.function(
    image=web_image,
    min_containers=MIN_CONTAINERS,
    max_containers=MAX_CONTAINERS,
    scaledown_window=SCALEDOWN_WINDOW,
    timeout=FUNCTION_TIMEOUT,
)
@modal.concurrent(max_inputs=10)
@modal.asgi_app(requires_proxy_auth=REQUIRES_PROXY_AUTH)
def web():
    import sys

    print(
        f"{PRESERVE_WEB_REQUIRES_PROXY_AUTH_ENV}="
        f"{'on' if REQUIRES_PROXY_AUTH else 'off'}, "
        f"{PRESERVE_WEB_SCALEDOWN_WINDOW_ENV}={SCALEDOWN_WINDOW}, "
        f"{PRESERVE_WEB_FUNCTION_TIMEOUT_ENV}={FUNCTION_TIMEOUT}, "
        f"min_containers={MIN_CONTAINERS}, max_containers={MAX_CONTAINERS}"
    )

    if REMOTE_SOURCE_DIR not in sys.path:
        sys.path.insert(0, REMOTE_SOURCE_DIR)

    from fastapi import FastAPI
    from gradio.routes import mount_gradio_app

    import preserve_model_gui as gui

    blocks = gui.build_interface(show_standalone_options=False)
    # download_model はジェネレータなのでキューを有効にする。
    blocks.queue()

    return mount_gradio_app(app=FastAPI(), blocks=blocks, path="/")
