import math
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
COMFYUI_REQUIRED_KITCHEN_CAPABILITIES_ENV: Final = (
    "COMFYUI_REQUIRED_KITCHEN_CAPABILITIES"
)
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
TORCH_WHEEL_URL: Final = (
    "https://download.pytorch.org/whl/cu130/"
    "torch-2.10.0%2Bcu130-cp312-cp312-manylinux_2_28_x86_64.whl"
)
TORCHVISION_WHEEL_URL: Final = (
    "https://download.pytorch.org/whl/cu130/"
    "torchvision-0.25.0%2Bcu130-cp312-cp312-manylinux_2_28_x86_64.whl"
)
TORCHAUDIO_WHEEL_URL: Final = (
    "https://download.pytorch.org/whl/cu130/"
    "torchaudio-2.10.0%2Bcu130-cp312-cp312-manylinux_2_28_x86_64.whl"
)
XFORMERS_WHEEL_URL: Final = (
    "https://download.pytorch.org/whl/cu130/"
    "xformers-0.0.34-cp39-abi3-manylinux_2_28_x86_64.whl"
)
FLASH_ATTN_WHEEL_URL: Final = (
    "https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/"
    "download/v0.9.0/"
    "flash_attn-2.8.3+cu130torch2.10-cp312-cp312-linux_x86_64.whl"
)
FLASH_ATTN_WHEEL_SHA256: Final = (
    "bb67bd3c2784ffedf07a2633b18722ccdaf54eb0aba18203fc02cae9ced44977"
)
FLASH_ATTN_WHEEL_FILENAME: Final = (
    "flash_attn-2.8.3+cu130torch2.10-cp312-cp312-linux_x86_64.whl"
)
SAGEATTENTION_REPOSITORY: Final = "https://github.com/woct0rdho/SageAttention.git"
SAGEATTENTION_COMMIT: Final = "93128b972683e52cd382c6731c1f09505e7524b5"
PREBUILT_WHEEL_DIR: Final = "/opt/prebuilt-wheels"
FLASH_ATTN_WHEEL_PATH: Final = (
    f"{PREBUILT_WHEEL_DIR}/{FLASH_ATTN_WHEEL_FILENAME}"
)
COMFYUI_COMMIT: Final = "024cbc5fc1c779ea7905356d3f3239b90dd0dae3"
COMFY_KITCHEN_VERSION: Final = "0.2.30"

GPU_PROFILES: Final = {
    "rtx5090": {
        "beam_gpu": "RTX5090",
        "cuda_arch_list": "12.0",
        "comfy_cuda_archs": "120f",
        "capacity": "serverless",
        "required_kitchen_capabilities": (
            "int8_linear,quantize_per_tensor_fp8,quantize_nvfp4,"
            "quantize_mxfp8,scaled_mm_nvfp4"
        ),
    },
    "rtx4090": {
        "beam_gpu": "RTX4090",
        "cuda_arch_list": "8.9",
        "comfy_cuda_archs": "89",
        "capacity": "serverless",
        "required_kitchen_capabilities": "int8_linear,quantize_per_tensor_fp8",
    },
    "rtx-pro-6000": {
        "beam_gpu": "RTXPro6000",
        "cuda_arch_list": "12.0",
        "comfy_cuda_archs": "120f",
        "capacity": "on-demand",
        "required_kitchen_capabilities": (
            "int8_linear,quantize_per_tensor_fp8,quantize_nvfp4,"
            "quantize_mxfp8,scaled_mm_nvfp4"
        ),
    },
    "h100": {
        "beam_gpu": "H100",
        "cuda_arch_list": "9.0",
        "comfy_cuda_archs": "90a",
        "capacity": "on-demand",
        "required_kitchen_capabilities": "int8_linear,quantize_per_tensor_fp8",
    },
    "a100-80gb": {
        "beam_gpu": "A100-80",
        "cuda_arch_list": "8.0",
        "comfy_cuda_archs": "80",
        "capacity": "on-demand",
        "required_kitchen_capabilities": "int8_linear",
    },
}

CUSTOM_NODES: Final = (
    (
        "ComfyUI-Crystools",
        "https://github.com/crystian/ComfyUI-Crystools.git",
        "2f18256c5b5063937106f29a8e0a7db3ae3869b7",
    ),
    (
        "ComfyUI_Local_Media_Manager",
        "https://github.com/Firetheft/ComfyUI_Local_Media_Manager.git",
        "5e74ce0cc708798ed25a77097d6059b6c796da87",
    ),
    (
        "ComfyUI-Image-Browsing",
        "https://github.com/hayden-fr/ComfyUI-Image-Browsing.git",
        "dd2e03e4815fa94c24e2820040cf75b9d4898805",
    ),
    (
        "rgthree-comfy",
        "https://github.com/rgthree/rgthree-comfy.git",
        "6b76ee6f2c5a007710b5a16f97c94330d6ecc871",
    ),
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
        raise ValueError(f"Invalid {env_name}: {raw!r}. Expected {minimum}..{maximum}.")
    return value


def _resolve_positive_float_env(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"Invalid {env_name}: {raw!r}. Expected a positive number."
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Invalid {env_name}: {raw!r}. Expected a positive number.")
    return value


def _verified_download_command(url: str, sha256: str, destination: str) -> str:
    """Build a command that downloads a file and verifies its SHA-256."""

    return (
        "set -eux; "
        f'mkdir -p "{Path(destination).parent}"; '
        f'wget -q "{url}" -O "{destination}"; '
        f'echo "{sha256}  {destination}" | sha256sum -c -'
    )


def _custom_node_install_command(name: str, repository: str, commit: str) -> str:
    """Clone and install one custom node at an audited commit."""

    destination = f"/root/comfy/ComfyUI/custom_nodes/{name}"
    return (
        "set -eux; "
        f'rm -rf "{destination}"; '
        f'git clone --filter=blob:none --no-checkout "{repository}" "{destination}"; '
        f'git -C "{destination}" checkout --detach "{commit}"; '
        f'git -C "{destination}" submodule update --init --recursive; '
        f'if [ -f "{destination}/requirements.txt" ]; then '
        f'python3 -m pip install --no-cache-dir -r "{destination}/requirements.txt"; '
        "fi"
    )


def _sage_attention_build_command(build_prefix: str) -> str:
    """Build SageAttention from the reviewed immutable commit."""

    return (
        "set -eux; "
        f"{build_prefix}"
        f'mkdir -p "{PREBUILT_WHEEL_DIR}"; '
        "rm -rf /tmp/SageAttention; "
        f'git clone --filter=blob:none --no-checkout "{SAGEATTENTION_REPOSITORY}" '
        '"/tmp/SageAttention"; '
        f'git -C /tmp/SageAttention checkout --detach "{SAGEATTENTION_COMMIT}"; '
        "git -C /tmp/SageAttention submodule update --init --recursive; "
        "cd /tmp/SageAttention; "
        "python3 -m build --wheel --no-isolation; "
        f'cp dist/*.whl "{PREBUILT_WHEEL_DIR}/"; '
        "rm -rf /tmp/SageAttention"
    )


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
REQUIRED_KITCHEN_CAPABILITIES = GPU_PROFILE["required_kitchen_capabilities"]
SAGE_ATTENTION_BUILD_PREFIX = (
    "export LIBRARY_PATH=/usr/local/cuda/lib64/stubs:${LIBRARY_PATH:-}; "
    if GPU_PROFILE_NAME == "h100"
    else ""
)
SAGE_ATTENTION_BUILD_COMMANDS = (
    [_sage_attention_build_command(SAGE_ATTENTION_BUILD_PREFIX)]
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
            (
                "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "git wget curl ca-certificates build-essential python3-dev pkg-config "
                "cmake ninja-build libgl1 libglib2.0-0 ffmpeg"
            ),
            (
                f'wget -q "{CUDA_KEYRING_URL}" -O /tmp/cuda-keyring.deb && '
                "dpkg -i /tmp/cuda-keyring.deb && "
                "apt-get update && DEBIAN_FRONTEND=noninteractive "
                "apt-get install -y cuda-toolkit-13-0 && "
                "rm -f /tmp/cuda-keyring.deb && rm -rf /var/lib/apt/lists/*"
            ),
            (
                "python3 -m pip install --no-cache-dir -U "
                "pip setuptools wheel build uv packaging ninja 'nanobind>=2.0.0' "
                "'cmake>=3.26'"
            ),
            (
                "python3 -m pip install --no-cache-dir "
                f'"{TORCH_WHEEL_URL}" "{TORCHVISION_WHEEL_URL}" '
                f'"{TORCHAUDIO_WHEEL_URL}" "{XFORMERS_WHEEL_URL}"'
            ),
            _verified_download_command(
                FLASH_ATTN_WHEEL_URL,
                FLASH_ATTN_WHEEL_SHA256,
                FLASH_ATTN_WHEEL_PATH,
            ),
            f'python3 -m pip install --no-cache-dir "{FLASH_ATTN_WHEEL_PATH}"',
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
            (
                # comfy-cli validates --commit against --version, which click resolves
                # in command-line order, so --version must come first.
                "comfy --skip-prompt install --nvidia --cuda-version 13.0 "
                f'--version nightly --commit "{COMFYUI_COMMIT}"'
            ),
            (
                "python3 -m pip install --no-cache-dir "
                f'"{TORCH_WHEEL_URL}" "{TORCHVISION_WHEEL_URL}" '
                f'"{TORCHAUDIO_WHEEL_URL}" "{XFORMERS_WHEEL_URL}"'
            ),
            (
                f'python3 -m pip install --no-cache-dir "{FLASH_ATTN_WHEEL_PATH}"'
                f"{SAGE_ATTENTION_INSTALL_TARGET}"
            ),
            (
                "set -eux; "
                "python3 -m pip install --no-cache-dir 'nvidia-cublas>=13.0.0'; "
                "python3 -m pip install --no-cache-dir --force-reinstall --no-deps "
                f"'comfy-kitchen=={COMFY_KITCHEN_VERSION}'; "
                "python3 -m pip show comfy-kitchen | "
                f"grep -F 'Version: {COMFY_KITCHEN_VERSION}'"
            ),
            *[
                _custom_node_install_command(name, repository, commit)
                for name, repository, commit in CUSTOM_NODES
            ],
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
        COMFYUI_REQUIRED_KITCHEN_CAPABILITIES_ENV: REQUIRED_KITCHEN_CAPABILITIES,
        COMFYUI_SAGE_ATTENTION_ENV: "on" if SAGE_ATTENTION_ENABLED else "off",
        "PYTHONUNBUFFERED": "1",
    },
    entrypoint=["python3", "beam_runtime.py"],
    keep_warm_seconds=KEEP_WARM_SECONDS,
    authorized=AUTHORIZED,
)
