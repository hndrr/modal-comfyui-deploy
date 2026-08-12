import os
import re
from pathlib import Path
from typing import Final

from beam import Image, Pod, Volume
from dotenv import load_dotenv


DOTENV_PATH: Final = Path(__file__).with_name(".env")
load_dotenv(DOTENV_PATH, override=False)

COMFYUI_BEAM_APP_NAME_ENV: Final = "COMFYUI_BEAM_APP_NAME"
COMFYUI_BEAM_AUTHORIZED_ENV: Final = "COMFYUI_BEAM_AUTHORIZED"
COMFYUI_BEAM_CPU_ENV: Final = "COMFYUI_BEAM_CPU"
COMFYUI_BEAM_GPU_ENV: Final = "COMFYUI_BEAM_GPU"
COMFYUI_BEAM_KEEP_WARM_SECONDS_ENV: Final = "COMFYUI_BEAM_KEEP_WARM_SECONDS"
COMFYUI_BEAM_MEMORY_ENV: Final = "COMFYUI_BEAM_MEMORY"
COMFYUI_BEAM_POOL_ENV: Final = "COMFYUI_BEAM_POOL"
COMFYUI_BEAM_SAGE_ATTENTION_ENV: Final = "COMFYUI_BEAM_SAGE_ATTENTION"
COMFYUI_BEAM_VOLUME_PREFIX_ENV: Final = "COMFYUI_BEAM_VOLUME_PREFIX"
COMFYUI_CLI_ARGS_ENV: Final = "COMFYUI_CLI_ARGS"
COMFYUI_EXPECTED_CUDA_ARCH_ENV: Final = "COMFYUI_EXPECTED_CUDA_ARCH"
COMFYUI_SAGE_ATTENTION_ENV: Final = "COMFYUI_SAGE_ATTENTION"

DEFAULT_APP_NAME: Final = "comfyui"
DEFAULT_CPU: Final = 12.0
DEFAULT_GPU_PROFILE: Final = "rtx5090"
DEFAULT_KEEP_WARM_SECONDS: Final = 30
DEFAULT_MEMORY: Final = "32Gi"
DEFAULT_VOLUME_PREFIX: Final = "comfyui"
MAX_KEEP_WARM_SECONDS: Final = 86400
MIN_KEEP_WARM_SECONDS: Final = -1

CUDA_KEYRING_URL: Final = (
    "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/"
    "cuda-keyring_1.1-1_all.deb"
)
PYTORCH_INDEX_URL: Final = "https://download.pytorch.org/whl/cu128"
FLASH_ATTN_WHEEL_URL: Final = (
    "https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/"
    "download/v0.9.0/"
    "flash_attn-2.8.3+cu128torch2.10-cp312-cp312-linux_x86_64.whl"
)
SAGEATTENTION_REF: Final = "abi3_stable"
PREBUILT_WHEEL_DIR: Final = "/opt/prebuilt-wheels"
COMFYUI_COMMIT: Final = "024cbc5fc1c779ea7905356d3f3239b90dd0dae3"
COMFY_KITCHEN_REPOSITORY: Final = "https://github.com/Comfy-Org/comfy-kitchen.git"
COMFY_KITCHEN_VERSION: Final = "0.2.30"

GPU_PROFILES: Final = {
    "rtx5090": {
        "beam_gpu": "RTX5090",
        "cuda_arch_list": "12.0",
        "comfy_cuda_archs": "120f",
        "capacity": "serverless",
    },
    "rtx4090": {
        "beam_gpu": "RTX4090",
        "cuda_arch_list": "8.9",
        "comfy_cuda_archs": "89",
        "capacity": "serverless",
    },
    "a10g": {
        "beam_gpu": "A10G",
        "cuda_arch_list": "8.6",
        "comfy_cuda_archs": "86",
        "capacity": "serverless",
    },
    "rtx-pro-6000": {
        "beam_gpu": "RTXPro6000",
        "cuda_arch_list": "12.0",
        "comfy_cuda_archs": "120f",
        "capacity": "on-demand",
    },
    "h100": {
        "beam_gpu": "H100",
        "cuda_arch_list": "9.0",
        "comfy_cuda_archs": "90a",
        "capacity": "on-demand",
    },
}

CUSTOM_NODES: Final = (
    "https://github.com/crystian/ComfyUI-Crystools",
    "https://github.com/Firetheft/ComfyUI_Local_Media_Manager",
    "https://github.com/hayden-fr/ComfyUI-Image-Browsing",
    "https://github.com/rgthree/rgthree-comfy",
)


def _resolve_switch(env_name: str, default: bool) -> bool:
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized == "on":
        return True
    if normalized == "off":
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
            f"Invalid {env_name}: {raw!r}. Expected {minimum}..{maximum}."
        ) from exc
    if not minimum <= value <= maximum:
        raise ValueError(
            f"Invalid {env_name}: {raw!r}. Expected {minimum}..{maximum}."
        )
    return value


def _resolve_positive_float_env(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {env_name}: {raw!r}. Expected a positive number.") from exc
    if value <= 0:
        raise ValueError(f"Invalid {env_name}: {raw!r}. Expected a positive number.")
    return value


def _resolve_name(env_name: str, default: str) -> str:
    value = os.environ.get(env_name, default).strip()
    if not value or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value):
        raise ValueError(
            f"Invalid {env_name}: {value!r}. Use lowercase letters, digits, and hyphens."
        )
    return value


def _resolve_optional_name(env_name: str) -> str | None:
    value = os.environ.get(env_name, "").strip()
    if not value:
        return None
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value):
        raise ValueError(
            f"Invalid {env_name}: {value!r}. Use lowercase letters, digits, and hyphens."
        )
    return value


def _resolve_gpu_profile() -> tuple[str, dict[str, str]]:
    profile_name = os.environ.get(COMFYUI_BEAM_GPU_ENV, DEFAULT_GPU_PROFILE)
    profile_name = profile_name.strip().lower()
    profile = GPU_PROFILES.get(profile_name)
    if profile is None:
        allowed = ", ".join(sorted(GPU_PROFILES))
        raise ValueError(
            f"Invalid {COMFYUI_BEAM_GPU_ENV}: {profile_name!r}. Allowed values: {allowed}"
        )
    return profile_name, profile


APP_NAME = _resolve_name(COMFYUI_BEAM_APP_NAME_ENV, DEFAULT_APP_NAME)
AUTHORIZED = _resolve_switch(COMFYUI_BEAM_AUTHORIZED_ENV, False)
CPU = _resolve_positive_float_env(COMFYUI_BEAM_CPU_ENV, DEFAULT_CPU)
GPU_PROFILE_NAME, GPU_PROFILE = _resolve_gpu_profile()
KEEP_WARM_SECONDS = _resolve_int_env(
    COMFYUI_BEAM_KEEP_WARM_SECONDS_ENV,
    DEFAULT_KEEP_WARM_SECONDS,
    MIN_KEEP_WARM_SECONDS,
    MAX_KEEP_WARM_SECONDS,
)
MEMORY = os.environ.get(COMFYUI_BEAM_MEMORY_ENV, DEFAULT_MEMORY).strip()
if not MEMORY:
    raise ValueError(f"Invalid {COMFYUI_BEAM_MEMORY_ENV}: memory must not be empty")
VOLUME_PREFIX = _resolve_name(COMFYUI_BEAM_VOLUME_PREFIX_ENV, DEFAULT_VOLUME_PREFIX)
BEAM_POOL = _resolve_optional_name(COMFYUI_BEAM_POOL_ENV)
SAGE_ATTENTION_ENABLED = _resolve_switch(COMFYUI_BEAM_SAGE_ATTENTION_ENV, False)
CUDA_ARCH_LIST = GPU_PROFILE["cuda_arch_list"]
COMFY_CUDA_ARCHS = GPU_PROFILE["comfy_cuda_archs"]
SAGE_ATTENTION_BUILD_PREFIX = (
    "export LIBRARY_PATH=/usr/local/cuda/lib64/stubs:${LIBRARY_PATH:-}; "
    if GPU_PROFILE_NAME == "h100"
    else ""
)
SAGE_ATTENTION_BUILD_COMMANDS = (
    [
        "set -eux; "
        f"{SAGE_ATTENTION_BUILD_PREFIX}"
        f'mkdir -p "{PREBUILT_WHEEL_DIR}"; '
        "rm -rf /tmp/SageAttention; "
        f'git clone --depth 1 --branch "{SAGEATTENTION_REF}" '
        "--recurse-submodules --shallow-submodules "
        "https://github.com/woct0rdho/SageAttention.git /tmp/SageAttention; "
        "cd /tmp/SageAttention; "
        "git submodule update --init --recursive; "
        "python3 -m build --wheel --no-isolation; "
        f'cp dist/*.whl "{PREBUILT_WHEEL_DIR}/"; '
        "rm -rf /tmp/SageAttention"
    ]
    if SAGE_ATTENTION_ENABLED
    else []
)
SAGE_ATTENTION_INSTALL_TARGET = (
    f' "{PREBUILT_WHEEL_DIR}"/*.whl' if SAGE_ATTENTION_ENABLED else ""
)

image = (
    # Keep Beam's default base image so Pod source sync includes beam_runtime.py.
    Image(python_version="python3.12")
    .with_envs(
        {
            "CUDA_HOME": "/usr/local/cuda",
            "COMFY_CUDA_ARCHS": COMFY_CUDA_ARCHS,
            "FORCE_CUDA": "1",
            "MAX_JOBS": "8",
            "NVCC_THREADS": "8",
            "SAGEATTENTION_CUDA_ARCH_LIST": CUDA_ARCH_LIST,
            "TORCH_CUDA_ARCH_LIST": CUDA_ARCH_LIST,
            "XFORMERS_IGNORE_FLASH_VERSION_CHECK": "1",
        }
    )
    .add_commands(
        [
            "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "git wget curl ca-certificates build-essential python3-dev pkg-config "
            "cmake ninja-build libgl1 libglib2.0-0 ffmpeg",
            f'wget -q "{CUDA_KEYRING_URL}" -O /tmp/cuda-keyring.deb && '
            "dpkg -i /tmp/cuda-keyring.deb && "
            "apt-get update && DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y cuda-toolkit-12-8 && "
            "rm -f /tmp/cuda-keyring.deb && rm -rf /var/lib/apt/lists/*",
            "python3 -m pip install --no-cache-dir -U "
            "pip setuptools wheel build uv packaging ninja 'nanobind>=2.0.0' "
            "'cmake>=3.26'",
            f"python3 -m pip install --no-cache-dir --index-url {PYTORCH_INDEX_URL} "
            "torch==2.10.0 torchvision==0.25.0 torchaudio==2.10.0 xformers==0.0.34",
            f'python3 -m pip install --no-cache-dir "{FLASH_ATTN_WHEEL_URL}"',
            *SAGE_ATTENTION_BUILD_COMMANDS,
        ]
    )
    .add_python_packages(
        [
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
        ]
    )
    .add_commands(
        [
            "comfy --skip-prompt install --nvidia --cuda-version 12.8 "
            f'--commit "{COMFYUI_COMMIT}"',
            f"python3 -m pip install --no-cache-dir --index-url {PYTORCH_INDEX_URL} "
            "torch==2.10.0 torchvision==0.25.0 torchaudio==2.10.0 xformers==0.0.34",
            f'python3 -m pip install --no-cache-dir "{FLASH_ATTN_WHEEL_URL}"'
            f"{SAGE_ATTENTION_INSTALL_TARGET}",
            "set -eux; "
            "python3 -m pip uninstall -y comfy-kitchen; "
            "rm -rf /tmp/comfy-kitchen; "
            f'git clone --depth 1 --branch "v{COMFY_KITCHEN_VERSION}" '
            f'"{COMFY_KITCHEN_REPOSITORY}" /tmp/comfy-kitchen; '
            "COMFY_CUDA_ARCHS=\"${COMFY_CUDA_ARCHS}\" "
            "python3 -m pip install --no-cache-dir --no-build-isolation --no-deps "
            "/tmp/comfy-kitchen; "
            "rm -rf /tmp/comfy-kitchen; "
            f"python3 -m pip show comfy-kitchen | grep -F 'Version: {COMFY_KITCHEN_VERSION}'",
            *[f'comfy node install "{node}"' for node in CUSTOM_NODES],
        ]
    )
)

volumes = [
    Volume(name=f"{VOLUME_PREFIX}-models", mount_path="/models"),
    Volume(name=f"{VOLUME_PREFIX}-custom-nodes", mount_path="/data/custom_nodes"),
    Volume(name=f"{VOLUME_PREFIX}-outputs", mount_path="/data/output"),
    Volume(name=f"{VOLUME_PREFIX}-inputs", mount_path="/data/input"),
    Volume(name=f"{VOLUME_PREFIX}-user-data", mount_path="/data/user"),
]

comfyui = Pod(
    app=APP_NAME,
    name=APP_NAME,
    image=image,
    ports=[8000],
    cpu=CPU,
    memory=MEMORY,
    gpu=GPU_PROFILE["beam_gpu"],
    gpu_count=1,
    pool=BEAM_POOL,
    volumes=volumes,
    env={
        COMFYUI_CLI_ARGS_ENV: os.environ.get(COMFYUI_CLI_ARGS_ENV, "").strip(),
        COMFYUI_EXPECTED_CUDA_ARCH_ENV: CUDA_ARCH_LIST,
        COMFYUI_SAGE_ATTENTION_ENV: "on" if SAGE_ATTENTION_ENABLED else "off",
        "PYTHONUNBUFFERED": "1",
    },
    entrypoint=["python3", "beam_runtime.py"],
    keep_warm_seconds=KEEP_WARM_SECONDS,
    authorized=AUTHORIZED,
)
