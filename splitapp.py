"""Deploy the split architecture separately: scripts/modal.sh deploy splitapp.py."""

import json
import os
import signal
import sys
import threading
import time
import uuid

import modal

from comfy_split import ambient_nodes
from comfy_split.storage import MOUNTS, VOLUME_NAMES
from comfyapp import (
    FLASH_ATTN_WHEEL_URL,
    FUNCTION_TIMEOUT,
    GPU_PROFILE,
    PREBUILT_WHEEL_DIR,
    SAGE_ATTENTION_ENABLED,
    TORCH_WHEEL_URL,
    TORCHAUDIO_WHEEL_URL,
    TORCHVISION_WHEEL_URL,
    XFORMERS_WHEEL_URL,
    base_image,
)

APP_NAME = "comfyui-split"
AMBIENT_MODE = ambient_nodes.enabled()
# Mint once on the deploying client and preserve it when Modal imports this
# module in a container. Keep it in the final image layer to reuse build caches.
AMBIENT_DEPLOYMENT = os.environ.get(ambient_nodes.DEPLOYMENT_ENV) or uuid.uuid4().hex
AMBIENT_SECRET_KEYS = (
    ("GEMINI_SECRET_NAME", "GEMINI_API_KEY"),
    ("TYPESAFE_SECRET_NAME", "TYPESAFE_API_KEY"),
    ("OPENROUTER_SECRET_NAME", "OPENROUTER_API_KEY"),
    ("AGENT_RUNTIME_SECRET_NAME", "AGENT_RUNTIME_BRIDGE_TOKEN"),
)
# Modal imports this module again inside each container. Preserve the names so
# that the remote function has exactly the same Secret dependencies as deploy.
# Only names belong in the image; credential values come from Modal Secrets.
secret_names = {
    name: os.environ.get(name, "").strip() for name, _ in AMBIENT_SECRET_KEYS
}
secret_names["GITHUB_SECRET_NAME"] = (
    os.environ.get("GITHUB_SECRET_NAME", "").strip() or "github-secret"
)
# The read-only repository token belongs only to the CPU updater. Provider keys
# and Bridge credentials reach the CPU endpoints and the GPU executing nodes.
ambient_secrets = (
    [
        modal.Secret.from_name(secret_name, required_keys=[key])
        for setting, key in AMBIENT_SECRET_KEYS
        if (secret_name := secret_names[setting])
    ]
    if AMBIENT_MODE
    else []
)
github_secrets = (
    [
        modal.Secret.from_name(
            secret_names["GITHUB_SECRET_NAME"],
            required_keys=[ambient_nodes.TOKEN_ENV],
        )
    ]
    if AMBIENT_MODE
    else []
)
COMFY_REVISION = "7a0b5eede3f9721c8faab290689893f36edc6d66"
FRONTEND_VERSION = "1.52.7"  # Version required by this ComfyUI revision.
MANAGER_VERSION = "4.2.2"
NODE_REVISIONS = {
    "crystian/ComfyUI-Crystools": "2f18256c5b5063937106f29a8e0a7db3ae3869b7",
    "Firetheft/ComfyUI_Local_Media_Manager": "5e74ce0cc708798ed25a77097d6059b6c796da87",
    "hayden-cn/ComfyUI-Image-Browsing": "3d0b5f8233d9d6b322ed3ff9a6cb15efbcf7bed7",  # v2.3.0
    "rgthree/rgthree-comfy": "2c5342a8cb0eaecaabf61435a5f37dd594c510ba",
    "Mozer/ComfyUI-MiniMax-H3-MotionCache-FastVAE": "b719329e0ecf35f0ae08d241c363ed1e56adbb95",
}
volumes = {
    key: modal.Volume.from_name(name, create_if_missing=True)
    for key, name in VOLUME_NAMES.items()
}
events = modal.Queue.from_name(APP_NAME + "-events", create_if_missing=True)
commands = modal.Queue.from_name(APP_NAME + "-commands", create_if_missing=True)

image = (
    base_image.run_commands(
        "git clone https://github.com/Comfy-Org/ComfyUI.git /opt/comfy-template",
        f"git -C /opt/comfy-template checkout {COMFY_REVISION}",
        "python -m pip install -r /opt/comfy-template/requirements.txt",
    )
    .pip_install(
        f"comfyui-frontend-package=={FRONTEND_VERSION}",
        f"comfyui-manager=={MANAGER_VERSION}",
        "modal==1.1.4",
        "aiohttp==3.12.15",
    )
    .run_commands(
        *[
            f"git clone https://github.com/{repo}.git /opt/comfy-template/custom_nodes/{repo.split('/')[1]} && "
            f"git -C /opt/comfy-template/custom_nodes/{repo.split('/')[1]} checkout {revision} && "
            f"if test -f /opt/comfy-template/custom_nodes/{repo.split('/')[1]}/requirements.txt; then "
            f"python -m pip install -r /opt/comfy-template/custom_nodes/{repo.split('/')[1]}/requirements.txt; fi"
            for repo, revision in NODE_REVISIONS.items()
        ]
    )
    # Bundle the matching published frontend on Modal, before containers start.
    # main's 2.3.1 revision requests an unpublished release and fails with 404.
    .run_commands(
        "curl --fail --location --retry 3 "
        "https://github.com/hayden-cn/ComfyUI-Image-Browsing/releases/download/v2.3.0/dist.tar.gz "
        "--output /tmp/image-browsing-dist.tar.gz",
        "echo 'c8b634911b8dbe69bc65b224eadf367b79e52841bd6825a94ef0dd1e40134a92  "
        "/tmp/image-browsing-dist.tar.gz' | sha256sum --check",
        "tar -xzf /tmp/image-browsing-dist.tar.gz "
        "-C /opt/comfy-template/custom_nodes/ComfyUI-Image-Browsing web/",
        "rm /tmp/image-browsing-dist.tar.gz",
    )
    .run_commands(
        f'python -m pip install "{TORCH_WHEEL_URL}" "{TORCHVISION_WHEEL_URL}" '
        f'"{TORCHAUDIO_WHEEL_URL}" "{XFORMERS_WHEEL_URL}" "{FLASH_ATTN_WHEEL_URL}" {PREBUILT_WHEEL_DIR}/*.whl',
        "python -m pip freeze > /opt/split-base-requirements.txt",
        "python -c 'import importlib.metadata as m; from pathlib import Path; "
        'names=["torch","torchvision","torchaudio","xformers","flash-attn","sageattention","comfyui-frontend-package","comfyui-manager","comfy-kitchen"]; '
        'Path("/opt/split-constraints.txt").write_text("\\n".join(n+"=="+m.version(n) for n in names)+"\\n")\'',
    )
    .env(
        {
            **secret_names,
            "SPLIT_APP": APP_NAME,
            "SPLIT_VOLUMES": json.dumps(VOLUME_NAMES),
            ambient_nodes.MODE_ENV: "on" if AMBIENT_MODE else "off",
            "COMFYUI_SAGE_ATTENTION": "on" if SAGE_ATTENTION_ENABLED else "off",
            "SPLIT_GENERATION_TIMEOUT": str(FUNCTION_TIMEOUT),
            "PYTHONPATH": "/opt/split",
        }
    )
    .add_local_file("comfyapp.py", "/root/comfyapp.py", copy=True)
    .add_local_dir(
        "extensions/ComfyUI-Modal-Control",
        "/opt/comfy-extensions/ComfyUI-Modal-Control",
        copy=True,
        ignore=["**/__pycache__/**", "**/*.pyc"],
    )
    .add_local_dir(
        "extensions/ComfyUI-Modal-Bridge",
        "/opt/comfy-extensions/ComfyUI-Modal-Bridge",
        copy=True,
        ignore=["**/__pycache__/**", "**/*.pyc"],
    )
    .add_local_dir(
        "comfy_split",
        "/opt/split/comfy_split",
        copy=True,
        ignore=["**/__pycache__/**", "**/*.pyc"],
    )
    .add_local_dir(
        "ambient",
        "/opt/split/ambient",
        copy=True,
        ignore=["docs/**", "**/__pycache__/**", "**/*.pyc"],
    )
    .run_commands(
        "python -m comfy_split.check_environment --requirements /opt/comfy-template/requirements.txt"
    )
    .env({ambient_nodes.DEPLOYMENT_ENV: AMBIENT_DEPLOYMENT})
)
app = modal.App(APP_NAME)

# Only this image preloads CPU ComfyUI during module import, before Modal's
# snapshot point. Keep the existing function identity, proxy auth and URL.
cpu_image = image.env({"SPLIT_CPU_MEMORY_SNAPSHOT": "1"})
if not modal.is_local() and os.environ.get("SPLIT_CPU_MEMORY_SNAPSHOT") == "1":
    from comfy_split.cpu_snapshot import prepare

    prepare()


@app.function(
    image=image,
    gpu=str(GPU_PROFILE["modal_gpu"]),
    secrets=ambient_secrets,
    min_containers=0,
    max_containers=1,
    scaledown_window=30,
    timeout=86400,
    retries=0,
    volumes={MOUNTS[key]: value for key, value in volumes.items()},
)
async def gpu_worker(spec):
    from comfy_split.worker import run_worker

    return await run_worker(spec, events, commands, volumes)


@app.function(
    image=cpu_image,
    min_containers=0,
    max_containers=1,
    scaledown_window=30,
    cpu=2,
    memory=8192,
    enable_memory_snapshot=True,
    startup_timeout=600,
    secrets=[*ambient_secrets, *github_secrets],
    timeout=86400,
    volumes={MOUNTS[key]: value for key, value in volumes.items()},
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app(requires_proxy_auth=True)
def ui():
    from modal._runtime.asgi import wait_for_web_server

    from comfy_split.cpu_snapshot import resume
    from comfy_split.modal_proxy import web_server_proxy

    gateway = resume()

    def monitor():
        time.sleep(10)
        while True:
            if gateway.poll() is not None:
                print(
                    f"Gateway process exited with code {gateway.returncode}, terminating container",
                    file=sys.stderr,
                    flush=True,
                )
                os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)

    threading.Thread(target=monitor, daemon=True).start()
    wait_for_web_server("127.0.0.1", 8000, timeout=600)
    return web_server_proxy("127.0.0.1", 8000)
