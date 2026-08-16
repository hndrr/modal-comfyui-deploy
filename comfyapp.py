import configparser
import filecmp
import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Final

import modal
from dotenv import dotenv_values, load_dotenv

volume = modal.Volume.from_name("comfy-model", create_if_missing=True)
custom_node_volume = modal.Volume.from_name(
    "comfy-custom-nodes", create_if_missing=True
)
output_volume = modal.Volume.from_name("comfy-outputs", create_if_missing=True)
input_volume = modal.Volume.from_name("comfy-inputs", create_if_missing=True)
user_data_volume = modal.Volume.from_name("comfy-user-data", create_if_missing=True)
MODEL_VOLUME_DIR = Path("/models")
COMFY_ROOT_CANDIDATES = [
    Path("/root/comfy/ComfyUI"),
    Path("/root/ComfyUI"),
    Path("/root/.cache/comfyui/ComfyUI"),
]
CUSTOM_NODE_VOLUME_MOUNT = Path("/data/custom_nodes")
OUTPUT_VOLUME_MOUNT = Path("/data/output")
INPUT_VOLUME_MOUNT = Path("/data/input")
USER_DATA_VOLUME_MOUNT = Path("/data/user")
WORKFLOWS_PATCH_MARKER = "# MODAL_PATCH_ALLOW_WORKFLOWS_START"
WORKFLOWS_PATCH_SNIPPET = textwrap.dedent(
    """
    # MODAL_PATCH_ALLOW_WORKFLOWS_START
    def _modal_allow_workflows():
        _candidates = (
            "ALLOWED_JSON_TYPES",
            "ALLOWED_TYPES",
            "ALLOWED_JSON_DIRS",
            "ALLOWED_DIRS",
        )
        for _name in _candidates:
            _container = globals().get(_name)
            if isinstance(_container, list):
                if "workflows" not in _container:
                    _container.append("workflows")
            elif isinstance(_container, set):
                if "workflows" not in _container:
                    _container.add("workflows")
            elif isinstance(_container, tuple):
                if "workflows" not in _container:
                    globals()[_name] = _container + ("workflows",)

        for _name in ("ALLOWED_JSON_TYPES_MAP", "ALLOWED_TYPES_MAP"):
            _mapping = globals().get(_name)
            if isinstance(_mapping, dict) and "workflows" not in _mapping:
                _mapping["workflows"] = "json"

    _modal_allow_workflows()
    del _modal_allow_workflows
    # MODAL_PATCH_ALLOW_WORKFLOWS_END
    """
).lstrip("\n")

TORCH_WHEEL_URL = "https://download.pytorch.org/whl/cu130/torch-2.10.0%2Bcu130-cp312-cp312-manylinux_2_28_x86_64.whl"
TORCHVISION_WHEEL_URL = "https://download.pytorch.org/whl/cu130/torchvision-0.25.0%2Bcu130-cp312-cp312-manylinux_2_28_x86_64.whl"
TORCHAUDIO_WHEEL_URL = "https://download.pytorch.org/whl/cu130/torchaudio-2.10.0%2Bcu130-cp312-cp312-manylinux_2_28_x86_64.whl"
# The cu130 index currently exposes a Python-3.12-compatible abi3 wheel at 0.0.34.
XFORMERS_WHEEL_URL = "https://download.pytorch.org/whl/cu130/xformers-0.0.34-cp39-abi3-manylinux_2_28_x86_64.whl"
FLASH_ATTN_WHEEL_URL = "https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.9.0/flash_attn-2.8.3+cu130torch2.10-cp312-cp312-linux_x86_64.whl"
SAGEATTENTION_REF = "abi3_stable"
COMFYUI_CLI_ARGS_ENV = "COMFYUI_CLI_ARGS"
COMFYUI_FORCE_BUILD_ENV = "COMFYUI_FORCE_BUILD"
COMFYUI_MANAGER_INSTALL_ENV = "COMFYUI_MANAGER_INSTALL"
COMFYUI_FUNCTION_TIMEOUT_ENV = "COMFYUI_FUNCTION_TIMEOUT"
COMFYUI_GPU_PROFILE_ENV = "COMFYUI_GPU_PROFILE"
COMFYUI_REQUIRES_PROXY_AUTH_ENV = "COMFYUI_REQUIRES_PROXY_AUTH"
COMFYUI_SAGE_ATTENTION_ENV = "COMFYUI_SAGE_ATTENTION"
COMFYUI_SCALEDOWN_WINDOW_ENV = "COMFYUI_SCALEDOWN_WINDOW"
DEFAULT_FUNCTION_TIMEOUT: Final = 1800
DEFAULT_SCALEDOWN_WINDOW: Final = 30
MAX_FUNCTION_TIMEOUT: Final = 86400
MAX_SCALEDOWN_WINDOW: Final = 1200
MIN_FUNCTION_TIMEOUT: Final = 1
MIN_SCALEDOWN_WINDOW: Final = 2
MIN_CONTAINERS: Final = 0
PREBUILT_WHEEL_DIR = "/opt/prebuilt-wheels"
SAGE_ATTENTION_FLAG = "--use-sage-attention"
DOTENV_PATH = Path(__file__).with_name(".env")


WEBSOCKET_COMPRESS_ORIGINAL = "web.WebSocketResponse()"
WEBSOCKET_COMPRESS_PATCHED = "web.WebSocketResponse(compress=False)"

# ComfyUI-Manager v4 は network_mode が personal_cloud のときだけノードパックの
# インストールを許す。既定の public では security_level が何であっても拒否される。
MANAGER_NETWORK_MODE_ALLOWED = "personal_cloud"
MANAGER_NETWORK_MODE_DENIED = "public"
MANAGER_SECURITY_LEVELS_ALLOWING_INSTALL = frozenset({"normal", "normal-", "weak"})


def configure_manager_install(comfy_root: Path, enabled: bool) -> None:
    """ComfyUI-Manager からのノードインストール可否を config.ini に反映する。

    Manager は起動時に 1 回だけ config.ini を読む。`user/` は Volume なので
    設定は永続する。off に戻したときに許可が残り続けないよう、無効化側も明示的に
    public へ書き戻す。
    """
    config_path = comfy_root / "user" / "__manager" / "config.ini"
    if not enabled and not config_path.exists():
        return

    parser = configparser.ConfigParser()
    if config_path.exists():
        try:
            parser.read(config_path, encoding="utf-8")
        except (OSError, configparser.Error) as exc:
            print(f"警告: {config_path} の読み込みに失敗しました: {exc}")
            return

    if not parser.has_section("default"):
        parser.add_section("default")

    desired_mode = (
        MANAGER_NETWORK_MODE_ALLOWED if enabled else MANAGER_NETWORK_MODE_DENIED
    )
    changed = parser.get("default", "network_mode", fallback=None) != desired_mode
    parser.set("default", "network_mode", desired_mode)

    if enabled:
        # security_level は Manager の既定が normal。strong だけは
        # personal_cloud でもインストールを拒むので、明示設定を尊重しつつ警告する。
        level = parser.get("default", "security_level", fallback=None)
        if level is None:
            parser.set("default", "security_level", "normal")
            changed = True
        elif level not in MANAGER_SECURITY_LEVELS_ALLOWING_INSTALL:
            print(
                f"警告: {config_path} の security_level={level} ではノードを"
                "インストールできません。normal 以下に変更してください。"
            )

    if not changed:
        return

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with config_path.open("w", encoding="utf-8") as handle:
            parser.write(handle)
    except OSError as exc:
        print(f"警告: {config_path} の書き込みに失敗しました: {exc}")
        return

    if enabled:
        print(
            f"{COMFYUI_MANAGER_INSTALL_ENV}=on: ComfyUI-Manager からの"
            "ノードインストールを許可しました（network_mode=personal_cloud）"
        )
    else:
        print(
            f"{COMFYUI_MANAGER_INSTALL_ENV}=off: ComfyUI-Manager からの"
            "ノードインストールを禁止しました（network_mode=public）"
        )


def patch_websocket_compression(comfy_root: Path) -> None:
    """ComfyUI の WebSocket から permessage-deflate を外す。

    Modal のプロキシは RFC 7692（permessage-deflate）に未対応で、圧縮を合意した
    接続をフレーム送出前に閉じる。ブラウザは既定で圧縮を提示するため、素のままだと
    `/ws` が 1 秒ごとに切れ続け、ComfyUI Manager のインストール進捗など push 通知が
    一切届かなくなる。ComfyUI 側に無効化フラグが無いので server.py を書き換える。
    """
    server_path = comfy_root / "server.py"
    if not server_path.exists():
        return

    try:
        content = server_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"警告: {server_path} の読み込みに失敗しました: {exc}")
        return

    if WEBSOCKET_COMPRESS_PATCHED in content:
        return

    if WEBSOCKET_COMPRESS_ORIGINAL not in content:
        print(
            f"警告: {server_path} に {WEBSOCKET_COMPRESS_ORIGINAL} が見つからず "
            "WebSocket 圧縮を無効化できませんでした。"
            "ComfyUI の更新で実装が変わった可能性があります。"
        )
        return

    updated = content.replace(
        WEBSOCKET_COMPRESS_ORIGINAL, WEBSOCKET_COMPRESS_PATCHED, 1
    )
    try:
        server_path.write_text(updated, encoding="utf-8")
        print(f"{server_path} の WebSocket 圧縮（permessage-deflate）を無効化しました")
    except OSError as exc:
        print(f"警告: {server_path} の書き込みに失敗しました: {exc}")


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

GPU_PROFILE_NAME: str
GPU_PROFILE: dict[str, str | bool]
COMFYUI_FORCE_BUILD: bool
FUNCTION_TIMEOUT: int
MANAGER_INSTALL_ENABLED: bool
REQUIRES_PROXY_AUTH: bool
SAGE_ATTENTION_ENABLED: bool
SCALEDOWN_WINDOW: int

GPU_PROFILES: Final = {
    "rtx-pro-6000": {
        "modal_gpu": "RTX-PRO-6000",
        "cuda_arch_list": "12.0+PTX",
    },
    "h100": {
        "modal_gpu": "H100",
        "cuda_arch_list": "9.0",
    },
    "a100-80gb": {
        "modal_gpu": "A100-80GB",
        "cuda_arch_list": "8.0",
    },
}


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


def _resolve_gpu_profile() -> tuple[str, dict[str, str | bool]]:
    profile_name = os.environ.get(COMFYUI_GPU_PROFILE_ENV, "rtx-pro-6000").strip()
    profile_name = profile_name.lower()
    profile = GPU_PROFILES.get(profile_name)
    if profile is None:
        allowed = ", ".join(sorted(GPU_PROFILES))
        raise ValueError(
            f"Invalid {COMFYUI_GPU_PROFILE_ENV}: {profile_name!r}. Allowed values: {allowed}"
        )
    return profile_name, profile


def _resolve_sage_attention_enabled() -> bool:
    raw = os.environ.get(COMFYUI_SAGE_ATTENTION_ENV, "on").strip().lower()
    if raw == "on":
        return True
    if raw == "off":
        return False
    raise ValueError(
        f"Invalid {COMFYUI_SAGE_ATTENTION_ENV}: {raw!r}. Allowed values: on, off"
    )


def _resolve_comfyui_force_build() -> bool:
    raw = os.environ.get(COMFYUI_FORCE_BUILD_ENV, "off").strip().lower()
    if raw == "on":
        return True
    if raw == "off":
        return False
    raise ValueError(
        f"Invalid {COMFYUI_FORCE_BUILD_ENV}: {raw!r}. Allowed values: on, off"
    )


def _resolve_manager_install_enabled() -> bool:
    raw = os.environ.get(COMFYUI_MANAGER_INSTALL_ENV, "off").strip().lower()
    if raw == "on":
        return True
    if raw == "off":
        return False
    raise ValueError(
        f"Invalid {COMFYUI_MANAGER_INSTALL_ENV}: {raw!r}. Allowed values: on, off"
    )


def _resolve_requires_proxy_auth() -> bool:
    raw = os.environ.get(COMFYUI_REQUIRES_PROXY_AUTH_ENV, "off").strip().lower()
    if raw == "on":
        return True
    if raw == "off":
        return False
    raise ValueError(
        f"Invalid {COMFYUI_REQUIRES_PROXY_AUTH_ENV}: {raw!r}. "
        "Allowed values: on, off"
    )


def _should_enable_sage_attention(cli_args: list[str]) -> bool:
    if not SAGE_ATTENTION_ENABLED:
        return False
    return SAGE_ATTENTION_FLAG not in cli_args


def _build_launch_command(extra_cli_args: str) -> list[str]:
    launch_command = [
        "comfy",
        "launch",
        "--",
        "--listen",
        "0.0.0.0",
        "--port",
        "8000",
        "--preview-method",
        "auto",
    ]
    cli_args = shlex.split(extra_cli_args) if extra_cli_args else []
    if _should_enable_sage_attention(cli_args):
        launch_command.append(SAGE_ATTENTION_FLAG)
    launch_command.extend(cli_args)
    return launch_command


GPU_PROFILE_NAME, GPU_PROFILE = _resolve_gpu_profile()
COMFYUI_FORCE_BUILD = _resolve_comfyui_force_build()
FUNCTION_TIMEOUT = _resolve_int_env(
    COMFYUI_FUNCTION_TIMEOUT_ENV,
    DEFAULT_FUNCTION_TIMEOUT,
    MIN_FUNCTION_TIMEOUT,
    MAX_FUNCTION_TIMEOUT,
)
MANAGER_INSTALL_ENABLED = _resolve_manager_install_enabled()
REQUIRES_PROXY_AUTH = _resolve_requires_proxy_auth()
SAGE_ATTENTION_ENABLED = _resolve_sage_attention_enabled()
SCALEDOWN_WINDOW = _resolve_int_env(
    COMFYUI_SCALEDOWN_WINDOW_ENV,
    DEFAULT_SCALEDOWN_WINDOW,
    MIN_SCALEDOWN_WINDOW,
    MAX_SCALEDOWN_WINDOW,
)
CUDA_ARCH_LIST = str(GPU_PROFILE["cuda_arch_list"])
SAGE_ATTENTION_BUILD_PREFIX = (
    "export LIBRARY_PATH=/usr/local/cuda/lib64/stubs:${LIBRARY_PATH:-}; "
    if GPU_PROFILE_NAME == "h100"
    else ""
)
# torch 2.10 の shim.h は aoti_torch_get_current_cuda_stream を #ifdef USE_CUDA で
# 囲んでいる。ビルドコンテナには GPU が無く torch が CUDA ランタイムを検出しないため
# USE_CUDA が付かず、SageAttention の utils.cuh が未定義参照でコンパイルに失敗する。
# setup.py は Windows でしか -DUSE_CUDA=1 を足さないので、公式の env フックで補う。
SAGE_ATTENTION_BUILD_PREFIX += "export NVCC_APPEND_FLAGS='-DUSE_CUDA=1'; "

# 使用するカスタムノードのリスト
NODES = [
    "https://github.com/crystian/ComfyUI-Crystools",
    "https://github.com/Firetheft/ComfyUI_Local_Media_Manager",
    "https://github.com/hayden-fr/ComfyUI-Image-Browsing",
    "https://github.com/rgthree/rgthree-comfy",
]

# イメージファイルの作成
base_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "git",
        "wget",
        "curl",
        "ca-certificates",
        "build-essential",
        "python3-dev",
        "pkg-config",
        "cmake",
        "ninja-build",
        "libgl1",
        "libglib2.0-0",
    )
    .env(
        {
            "CUDA_HOME": "/usr/local/cuda",
            "XFORMERS_IGNORE_FLASH_VERSION_CHECK": "1",
            "TORCH_CUDA_ARCH_LIST": CUDA_ARCH_LIST,
            "SAGEATTENTION_CUDA_ARCH_LIST": CUDA_ARCH_LIST,
            "MAX_JOBS": "8",
            "NVCC_THREADS": "8",
            "FORCE_CUDA": "1",
        }
    )
    .run_commands(
        # CUDA 13.0（nvcc）導入
        "set -eux; "
        "wget https://developer.download.nvidia.com/compute/cuda/repos/debian12/x86_64/cuda-keyring_1.1-1_all.deb; "
        "dpkg -i cuda-keyring_1.1-1_all.deb; "
        "apt-get update; "
        "apt-get install -y cuda-toolkit-13-0"
    )
    .run_commands(
        # comfy-docker と同じ Torch/cu130 系を先に入れる
        "set -eux; "
        "python3 -m pip install --no-cache-dir -U pip setuptools wheel build uv packaging ninja; "
        f'python3 -m pip install --no-cache-dir "{TORCH_WHEEL_URL}" "{TORCHVISION_WHEEL_URL}" "{TORCHAUDIO_WHEEL_URL}" "{XFORMERS_WHEEL_URL}" "{FLASH_ATTN_WHEEL_URL}"'
    )
    .run_commands(
        # SageAttention は先に wheel build して退避しておく
        "set -eux; "
        f"{SAGE_ATTENTION_BUILD_PREFIX}"
        f'mkdir -p "{PREBUILT_WHEEL_DIR}"; '
        "rm -rf /tmp/SageAttention; "
        f'git clone --depth 1 --branch "{SAGEATTENTION_REF}" --recurse-submodules --shallow-submodules https://github.com/woct0rdho/SageAttention.git /tmp/SageAttention; '
        "cd /tmp/SageAttention; "
        "git submodule update --init --recursive; "
        "python3 -m build --wheel --no-isolation; "
        f'cp dist/*.whl "{PREBUILT_WHEEL_DIR}/"; '
        "rm -rf /tmp/SageAttention"
    )
)

image = (
    base_image
    .pip_install(
        "comfy-cli==1.7.3",
        "diffusers==0.32.0",
        "moviepy==1.0.3",
        "librosa==0.10.2.post1",
        "soundfile==0.12.1",
        "ftfy==6.2.3",
        "matplotlib",
        "onnxruntime-gpu",
        "scikit-image",
        "accelerate==1.1.0",
        "gguf",
        "taichi>=1.6,<1.8",
    )
    .run_commands(
        "comfy --skip-prompt install --nvidia",
        force_build=COMFYUI_FORCE_BUILD,
    )
    .run_commands(
        # comfy install が依存を触っても最終的には cu130 + prebuilt SageAttention で揃える
        "set -eux; "
        "python3 -m pip install --no-cache-dir -U pip setuptools wheel build uv packaging ninja; "
        f'python3 -m pip install --no-cache-dir "{TORCH_WHEEL_URL}" "{TORCHVISION_WHEEL_URL}" "{TORCHAUDIO_WHEEL_URL}" "{XFORMERS_WHEEL_URL}" "{FLASH_ATTN_WHEEL_URL}" {PREBUILT_WHEEL_DIR}/*.whl'
    )
    .run_commands(*[f"comfy node install {node}" for node in NODES])
)

app = modal.App(name="comfyui", image=image)


@app.function(
    min_containers=MIN_CONTAINERS,
    max_containers=1,
    scaledown_window=SCALEDOWN_WINDOW,
    timeout=FUNCTION_TIMEOUT,
    gpu=str(GPU_PROFILE["modal_gpu"]),
    # コンテナには comfyapp.py だけが送られ `.env` は入らない。デプロイ時に
    # 解決した値をここで渡さないと、コンテナ内では既定値に戻ってしまう。
    secrets=[
        modal.Secret.from_dict(
            {COMFYUI_MANAGER_INSTALL_ENV: "on" if MANAGER_INSTALL_ENABLED else "off"}
        )
    ],
    volumes={
        MODEL_VOLUME_DIR.as_posix(): volume,
        CUSTOM_NODE_VOLUME_MOUNT.as_posix(): custom_node_volume,
        OUTPUT_VOLUME_MOUNT.as_posix(): output_volume,
        INPUT_VOLUME_MOUNT.as_posix(): input_volume,
        USER_DATA_VOLUME_MOUNT.as_posix(): user_data_volume,
    },
)
@modal.concurrent(max_inputs=10)
@modal.web_server(
    8000,
    startup_timeout=60,
    requires_proxy_auth=REQUIRES_PROXY_AUTH,
)
def ui():
    print(
        f"{COMFYUI_GPU_PROFILE_ENV}={GPU_PROFILE_NAME} "
        f"({GPU_PROFILE['modal_gpu']}), "
        f"TORCH_CUDA_ARCH_LIST={CUDA_ARCH_LIST}, "
        f"{COMFYUI_SAGE_ATTENTION_ENV}={'on' if SAGE_ATTENTION_ENABLED else 'off'}, "
        f"{COMFYUI_FORCE_BUILD_ENV}={'on' if COMFYUI_FORCE_BUILD else 'off'}, "
        f"{COMFYUI_REQUIRES_PROXY_AUTH_ENV}="
        f"{'on' if REQUIRES_PROXY_AUTH else 'off'}, "
        f"min_containers={MIN_CONTAINERS}, "
        f"{COMFYUI_SCALEDOWN_WINDOW_ENV}={SCALEDOWN_WINDOW}, "
        f"{COMFYUI_FUNCTION_TIMEOUT_ENV}={FUNCTION_TIMEOUT}"
    )

    CUSTOM_NODE_VOLUME_MOUNT.mkdir(parents=True, exist_ok=True)
    OUTPUT_VOLUME_MOUNT.mkdir(parents=True, exist_ok=True)
    INPUT_VOLUME_MOUNT.mkdir(parents=True, exist_ok=True)
    MODEL_VOLUME_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_VOLUME_MOUNT.mkdir(parents=True, exist_ok=True)

    comfy_roots = [root_dir for root_dir in COMFY_ROOT_CANDIDATES if root_dir.exists()]
    if not comfy_roots:
        # どの候補も存在しない場合は最初の候補を作成ターゲットとして扱う。
        comfy_roots.append(COMFY_ROOT_CANDIDATES[0])

    def _merge_directory_contents(source_dir: Path, target_dir: Path) -> None:
        """対象ディレクトリの中身をソースディレクトリへ統合する"""

        for item in list(target_dir.iterdir()):
            destination = source_dir / item.name

            if item.is_dir():
                if destination.exists():
                    if destination.is_dir():
                        shutil.copytree(item, destination, dirs_exist_ok=True)
                        shutil.rmtree(item)
                    else:
                        backup = destination.with_suffix(".dir_conflict")
                        shutil.move(str(item), backup)
                else:
                    shutil.move(str(item), destination)
            else:
                if destination.exists():
                    try:
                        same_file = destination.is_file() and filecmp.cmp(
                            item, destination, shallow=False
                        )
                    except OSError:
                        same_file = False
                    if same_file:
                        item.unlink()
                    else:
                        backup = destination.with_suffix(".conflict")
                        shutil.move(str(item), backup)
                else:
                    shutil.move(str(item), destination)

    def patch_user_manager_for_workflows(comfy_root: Path) -> None:
        """ComfyUI のユーザーデータ API を補正し workflows 保存を許可する"""
        candidate_paths = [
            comfy_root / "comfy" / "ui" / "user_manager.py",
            comfy_root / "app" / "user_manager.py",
        ]

        replacements = {
            '@routes.get("/userdata/{file}")': '@routes.get(r"/userdata/{file:.*}")',
            "@routes.get('/userdata/{file}')": "@routes.get(r'/userdata/{file:.*}')",
            '@routes.post("/userdata/{file}")': '@routes.post(r"/userdata/{file:.*}")',
            "@routes.post('/userdata/{file}')": "@routes.post(r'/userdata/{file:.*}')",
            '@routes.delete("/userdata/{file}")': '@routes.delete(r"/userdata/{file:.*}")',
            "@routes.delete('/userdata/{file}')": "@routes.delete(r'/userdata/{file:.*}')",
            '@routes.post("/userdata/{file}/move/{dest}")': '@routes.post(r"/userdata/{file:.*}/move/{dest:.*}")',
            "@routes.post('/userdata/{file}/move/{dest}')": "@routes.post(r'/userdata/{file:.*}/move/{dest:.*}')",
        }

        for user_manager_path in candidate_paths:
            if not user_manager_path.exists():
                continue

            try:
                content = user_manager_path.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"警告: {user_manager_path} の読み込みに失敗しました: {exc}")
                continue

            updated = content
            modified = False

            for original, replacement in replacements.items():
                if replacement not in updated and original in updated:
                    updated = updated.replace(original, replacement)
                    modified = True

            if WORKFLOWS_PATCH_MARKER not in updated:
                updated = f"{updated}\n{WORKFLOWS_PATCH_SNIPPET}"
                modified = True

            if not modified:
                continue

            try:
                user_manager_path.write_text(updated, encoding="utf-8")
                print(f"{user_manager_path} に workflows 保存許可パッチを適用しました")
            except OSError as exc:
                print(f"警告: {user_manager_path} の書き込みに失敗しました: {exc}")

    def link_directory(target: Path, source: Path) -> bool:
        """指定ディレクトリを永続化 Volume に向ける"""

        source.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.is_symlink():
            current_target = Path(os.readlink(target))
            if current_target != source:
                target.unlink()
                target.symlink_to(source, target_is_directory=True)
            return True

        if target.exists():
            if target.is_dir():
                _merge_directory_contents(source, target)
                if any(target.iterdir()):
                    print(
                        f"警告: {target} を空にできなかったためシンボリックリンクを作成しません"
                    )
                    return False
                target.rmdir()
                target.symlink_to(source, target_is_directory=True)
                return True

            print(
                f"警告: {target} は既存ファイルのためシンボリックリンクを作成しません"
            )
            return False

        target.symlink_to(source, target_is_directory=True)
        return True

    for comfy_root in comfy_roots:
        patch_user_manager_for_workflows(comfy_root)
        patch_websocket_compression(comfy_root)
        models_dir = comfy_root / "models"

        if link_directory(models_dir, MODEL_VOLUME_DIR):
            print(f"{models_dir} を {MODEL_VOLUME_DIR} に接続しました")

        if link_directory(comfy_root / "custom_nodes", CUSTOM_NODE_VOLUME_MOUNT):
            print(f"{comfy_root} の custom_nodes を永続化 Volume に接続しました")

        if link_directory(comfy_root / "output", OUTPUT_VOLUME_MOUNT):
            print(f"{comfy_root} の output を永続化 Volume に接続しました")

        if link_directory(comfy_root / "input", INPUT_VOLUME_MOUNT):
            print(f"{comfy_root} の input を永続化 Volume に接続しました")

        if link_directory(comfy_root / "user", USER_DATA_VOLUME_MOUNT):
            print(f"{comfy_root} の user ディレクトリを永続化 Volume に接続しました")

        # config.ini は user/ 配下なので、Volume へ繋いだ後に書く。
        # 値は Secret 経由で注入されるため、モジュール読み込み時ではなく
        # ここで読み直す（import と Secret 注入の順序に依存しないため）。
        configure_manager_install(comfy_root, _resolve_manager_install_enabled())

    extra_cli_args = os.environ.get(COMFYUI_CLI_ARGS_ENV, "").strip()
    launch_command = _build_launch_command(extra_cli_args)

    if extra_cli_args:
        print(f"{COMFYUI_CLI_ARGS_ENV}={extra_cli_args}")

    subprocess.Popen(launch_command)
