"""preserve_model_gui.py の Gradio UI を Modal 上の Web アプリとして公開する

`preserve_model_gui.py` はローカル実行を前提にした CLI 兼 GUI である。
このファイルはそれを **デプロイするためだけ** の薄いラッパーで、UI の組み立ては
`preserve_model_gui.build_model_import_panel()` に任せる。

コンテナ内では次の 2 点がローカル実行と異なる。

1. `preserve_model_gui` の既定モード（`modal.App.run()` で一時コンテナを立てる）は
   使えない。Modal が「Can not run an app in global scope within a container」を
   投げるため、デプロイ済み関数を呼ぶモードに固定する。
2. Modal の認証はコンテナ ID で自動的に通るので、`modal token` によるログインも
   トークンの受け渡しも不要になる。

Modal 上の App は `preserve-model` 1 つで、そこに 2 つの Function が並ぶ。

- `preserve_model`: Hugging Face からダウンロードして Volume に保存する（`preserve_model.py`）
- `web`: この GUI

**デプロイの入口はこのファイルだけ。**

    PRESERVE_WEB_REQUIRES_PROXY_AUTH=on uv run modal deploy preserve_model_web.py

`modal deploy preserve_model.py` を実行すると、その時点で定義されている Function
だけが残るため **web 関数が消える**。ダウンロード関数だけを更新したい場合も、
このファイルからデプロイすること。

ローカルで GUI を動かす場合はデプロイ不要（`uv run preserve_model_gui.py`）。
その既定モードは `modal.App.run()` で一時コンテナを立てるため、
このファイルにも `preserve-model` のデプロイにも依存しない。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import modal

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - コンテナ内のみ通る経路
    # `.env` を読むのは `modal deploy` を叩くローカル側だけ。コンテナには
    # `.env` も python-dotenv も無いので、無ければ何もしない実装で代替する。
    # 解決済みの設定値はイメージの環境変数として焼き込んである。
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

# App と preserve_model 関数を持ち込む。新しい App は作らず、こちらに相乗りする。
# こうすると Modal 上の App は `preserve-model` 1 つで済み、
# preserve_model 関数と web 関数が同じ App に並ぶ。
import preserve_model

# コンテナ内で preserve_model_gui.py を置くディレクトリ。
# preserve_model_gui.py は Path(__file__).with_name("preserve_model.py") で
# preserve_model.py を読み込むため、2 つは同じディレクトリに置く必要がある。
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

# preserve_model 側が max_containers=1 で I/O 競合を避けているので web 側も 1 で揃える
MAX_CONTAINERS: Final = 1
MIN_CONTAINERS: Final = 0

DOTENV_PATH = Path(__file__).with_name(".env")

load_dotenv(DOTENV_PATH, override=False)

REQUIRES_PROXY_AUTH: bool
SCALEDOWN_WINDOW: int
FUNCTION_TIMEOUT: int


def _resolve_on_off_env(env_name: str, default: str = "off") -> bool:
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


# 設定が不正ならイメージのビルド前にここで落とす
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

# この関数用のイメージ。GPU も Volume も不要で、GUI を出すだけ。
# 実際のダウンロードは同じ App の preserve_model 関数が行い、
# そちらが comfy-model Volume と huggingface-secret を持っている。
web_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(f"gradio=={GRADIO_VERSION}", f"modal=={MODAL_VERSION}")
    # コンテナ内でもモジュールが再 import され設定が再解決されるため、
    # デプロイ時に確定した値を焼き込んでおく。こうしないと起動ログが
    # 既定値になって実際の設定と食い違う。
    .env(
        {
            PRESERVE_WEB_REQUIRES_PROXY_AUTH_ENV: "on" if REQUIRES_PROXY_AUTH else "off",
            PRESERVE_WEB_SCALEDOWN_WINDOW_ENV: str(SCALEDOWN_WINDOW),
            PRESERVE_WEB_FUNCTION_TIMEOUT_ENV: str(FUNCTION_TIMEOUT),
        }
    )
    .add_local_file(
        Path(__file__).with_name("preserve_model.py").as_posix(),
        f"{REMOTE_SOURCE_DIR}/preserve_model.py",
    )
    .add_local_file(
        Path(__file__).with_name("preserve_model_gui.py").as_posix(),
        f"{REMOTE_SOURCE_DIR}/preserve_model_gui.py",
    )
)

# 既存の App（preserve-model）に相乗りする。App 名は preserve_model.py 側の定義に従う。
# イメージは Function ごとに指定できるので、ダウンロード用の軽量イメージとは別物でよい。
app = preserve_model.app


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

    import gradio as gr
    from fastapi import FastAPI
    from gradio.routes import mount_gradio_app

    import preserve_model_gui as gui

    # コンテナ内では app.run() による一時コンテナ起動が使えないため、
    # デプロイ済み関数を呼ぶモードに固定する。
    gui.CONFIG.use_deployed = True

    with gr.Blocks(title="Modal: Hugging Face モデル取り込み") as blocks:
        gui.build_model_import_panel(show_standalone_options=False)

    # download_model はジェネレータなのでキューが要る。
    # mount_gradio_app はキュー設定の検証はするが有効化はしない。
    blocks.queue()

    return mount_gradio_app(app=FastAPI(), blocks=blocks, path="/")
