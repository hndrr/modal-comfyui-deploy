"""Hugging Face モデルの保存処理と、その Gradio Web UI の Modal App。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Final, Optional

import modal

try:
    from dotenv import dotenv_values, load_dotenv
except ModuleNotFoundError:  # pragma: no cover - Modal コンテナ内のみ通る経路
    # `.env` を読むのはデプロイを実行するローカル側だけ。
    # 解決済みの設定値は
    # Web UI イメージの環境変数として渡すため、コンテナでは何もしない。
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

    def dotenv_values(*_args, **_kwargs) -> dict[str, str | None]:
        return {}

# create a Volume, or retrieve it if     it exists
volume = modal.Volume.from_name("comfy-model", create_if_missing=True)

# 進捗の受け渡し用。コンテナの print は Modal のログにしか出ないので、
# 呼び出し側（GUI / CLI）が読める場所に FunctionCall ID をキーとして書く。
PROGRESS_DICT_NAME: Final = "preserve-model-progress"
PROGRESS_POLL_SECONDS: Final = 3.0
PROGRESS_STALE_SECONDS: Final = 60 * 60 * 6
progress_dict = modal.Dict.from_name(PROGRESS_DICT_NAME, create_if_missing=True)

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


def _publish_progress(call_id: str, payload: dict[str, Any]) -> None:
    """進捗を Dict に書き、Modal のログにも同じ内容を残す。

    表示のためだけの経路なので、書き込みに失敗しても保存処理は続行する。
    """
    line = " ".join(f"{key}={value}" for key, value in payload.items())
    print(f"[progress] {line}", flush=True)
    if not call_id:
        return
    try:
        progress_dict[call_id] = payload
    except Exception as exc:  # pragma: no cover - 進捗表示の失敗で本処理は止めない
        print(f"[progress] 書き込みに失敗しました: {exc}", flush=True)


def _remote_file_size(
    repo_id: str, filename: str, revision: Optional[str]
) -> Optional[int]:
    """ダウンロード前に総バイト数を引く。取れなくても進捗表示を諦めるだけ。"""
    try:
        from huggingface_hub import get_hf_file_metadata, hf_hub_url

        url = hf_hub_url(repo_id=repo_id, filename=filename, revision=revision)
        return get_hf_file_metadata(url).size
    except Exception:  # pragma: no cover - メタデータ取得はベストエフォート
        return None


def _incomplete_download_bytes() -> Optional[int]:
    """HF キャッシュに出る `*.incomplete` のサイズ（= ダウンロード済み量）。"""
    cache_root = Path(
        os.environ.get("HF_HUB_CACHE")
        or os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")
    )
    try:
        sizes = [path.stat().st_size for path in cache_root.rglob("*.incomplete")]
    except OSError:  # pragma: no cover - キャッシュ未作成など
        return None
    return max(sizes) if sizes else None


def _prune_progress(now: float) -> None:
    """Dict に残り続ける古い進捗を落とす。"""
    try:
        stale = [
            key
            for key, value in progress_dict.items()
            if now - float((value or {}).get("updated_at", 0.0)) > PROGRESS_STALE_SECONDS
        ]
    except Exception:  # pragma: no cover - 掃除できなくても実害はない
        return
    for key in stale:
        try:
            # Modal の Dict.pop は既定値を取らない。他の実行と競合して
            # 既に消えていることがあるので KeyError は無視する。
            progress_dict.pop(key)
        except KeyError:
            pass
        except Exception:  # pragma: no cover - 掃除できなくても実害はない
            return


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

    call_id = modal.current_function_call_id() or ""
    started_at = time.time()
    _prune_progress(started_at)

    def report(phase: str, **fields: Any) -> None:
        _publish_progress(
            call_id,
            {
                "phase": phase,
                "updated_at": time.time(),
                "started_at": started_at,
                "repo_id": repo_id,
                "filename": filename,
                **fields,
            },
        )

    filename_path = Path(filename)
    try:
        destination_path = _resolve_destination(filename, destination_subdir)
        report("preparing", destination=destination_path.as_posix())

        total_bytes = _remote_file_size(repo_id, filename_path.as_posix(), revision)
        report("downloading", downloaded_bytes=0, total_bytes=total_bytes)

        # hf_hub_download は進捗コールバックを持たないので、キャッシュに現れる
        # `*.incomplete` のサイズを別スレッドで監視して代用する。
        stop_watching = threading.Event()

        def _watch_download() -> None:
            while not stop_watching.wait(PROGRESS_POLL_SECONDS):
                downloaded = _incomplete_download_bytes()
                if downloaded is not None:
                    report(
                        "downloading",
                        downloaded_bytes=downloaded,
                        total_bytes=total_bytes,
                    )

        watcher = threading.Thread(target=_watch_download, daemon=True)
        watcher.start()
        try:
            downloaded_path = Path(
                hf_hub_download(
                    repo_id=repo_id,
                    filename=filename_path.as_posix(),
                    revision=revision,
                    local_dir_use_symlinks=False,
                    resume_download=True,
                )
            )
        finally:
            stop_watching.set()
            watcher.join(timeout=PROGRESS_POLL_SECONDS)

        if downloaded_path.resolve() != destination_path.resolve():
            report("copying", destination=destination_path.as_posix())
            shutil.copy2(downloaded_path, destination_path)
            downloaded_path = destination_path
        file_stat = downloaded_path.stat()
        completed_at = datetime.now(timezone.utc).isoformat()
        print(f"モデルファイルを {downloaded_path} に保存しました")
        report(
            "done",
            destination=destination_path.as_posix(),
            size_bytes=file_stat.st_size,
            completed_at=completed_at,
        )
        return {
            "destination": destination_path.as_posix(),
            "size_bytes": file_stat.st_size,
            "completed_at": completed_at,
        }
    except Exception as exc:
        report("error", message=f"{type(exc).__name__}: {exc}")
        raise


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


def _reject_dotenv_modal_profile(dotenv_path: Path, shell_value: str | None) -> None:
    """`.env` に書かれた MODAL_PROFILE を拒否する。

    modal は `import modal` の時点で使用プロファイルを確定するため、その後に走る
    `load_dotenv()` の値は無視される。放置すると意図しない Modal アカウントへ
    黙ってデプロイされるので、ここで止める。詳細は docs/modal-profiles.md。
    """
    if not dotenv_path.exists():
        return
    # 値の解釈は load_dotenv と同じパーサーに任せる。自前で分解すると
    # インラインコメントや引用符の扱いがズレて誤検知になる。
    # ただし interpolate=False にする。`${VAR}` を展開すると
    # load_dotenv(override=False) と結果がズレるため、書かれた文字列のまま見る。
    raw = dotenv_values(dotenv_path, interpolate=False).get("MODAL_PROFILE")
    wanted = (raw or "").strip()
    if wanted and wanted != (shell_value or ""):
        raise RuntimeError(
            f"{dotenv_path.name} の MODAL_PROFILE={wanted} は modal に渡りません"
            "（プロファイルは modal の import 時に確定するため）。"
            f"その行を消して、MODAL_PROFILE={wanted} uv run modal ... または"
            f" ./scripts/modal.sh --profile {wanted} ... を使ってください。"
        )


# `.env` は MODAL_PROFILE を上書きできないので、読み込む前の値を控えておく。
_SHELL_MODAL_PROFILE = os.environ.get("MODAL_PROFILE")
load_dotenv(DOTENV_PATH, override=False)
_reject_dotenv_modal_profile(DOTENV_PATH, _SHELL_MODAL_PROFILE)


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
