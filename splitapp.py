"""Deploy the split architecture separately: scripts/modal.sh deploy splitapp.py."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import modal

from comfyapp import (
    base_image, FUNCTION_TIMEOUT, GPU_PROFILE, SAGE_ATTENTION_ENABLED,
    TORCH_WHEEL_URL, TORCHVISION_WHEEL_URL, TORCHAUDIO_WHEEL_URL,
    XFORMERS_WHEEL_URL, FLASH_ATTN_WHEEL_URL, PREBUILT_WHEEL_DIR,
)

APP_NAME = "comfyui-split"
COMFY_REVISION = "5bbdf8a76678e2c7cfb519a49a9c3a7137fd6280"
FRONTEND_VERSION = "1.51.10"  # Version required by this ComfyUI revision.
MANAGER_VERSION = "4.2.2"
NODE_REVISIONS = {
    "crystian/ComfyUI-Crystools": "2f18256c5b5063937106f29a8e0a7db3ae3869b7",
    "Firetheft/ComfyUI_Local_Media_Manager": "5e74ce0cc708798ed25a77097d6059b6c796da87",
    "hayden-cn/ComfyUI-Image-Browsing": "3d0b5f8233d9d6b322ed3ff9a6cb15efbcf7bed7",  # v2.3.0
    "rgthree/rgthree-comfy": "2c5342a8cb0eaecaabf61435a5f37dd594c510ba",
}
VOLUME_NAMES = {
    "models": "comfy-model", "input": "comfy-inputs", "output": "comfy-outputs",
    "user": "comfy-split-user-data", "seed_user": "comfy-user-data", "nodes": "comfy-custom-nodes",
    "state": "comfy-split-state", "results": "comfy-split-results",
    "environment": "comfy-split-environments",
}
MOUNTS = {"models": "/models", "input": "/data/input", "output": "/data/output",
          "user": "/data/user", "nodes": "/data/custom_nodes", "state": "/state",
          "results": "/results", "environment": "/environments", "seed_user": "/seed/user"}
volumes = {key: modal.Volume.from_name(name, create_if_missing=True)
           for key, name in VOLUME_NAMES.items()}
events = modal.Queue.from_name(APP_NAME + "-events", create_if_missing=True)
commands = modal.Queue.from_name(APP_NAME + "-commands", create_if_missing=True)

image = (
    base_image
    .run_commands(
        "git clone https://github.com/Comfy-Org/ComfyUI.git /opt/comfy-template",
        f"git -C /opt/comfy-template checkout {COMFY_REVISION}",
        "python -m pip install -r /opt/comfy-template/requirements.txt",
    )
    .pip_install(f"comfyui-frontend-package=={FRONTEND_VERSION}",
                 f"comfyui-manager=={MANAGER_VERSION}", "modal==1.1.4", "aiohttp==3.12.15")
    .run_commands(*[
        f"git clone https://github.com/{repo}.git /opt/comfy-template/custom_nodes/{repo.split('/')[1]} && "
        f"git -C /opt/comfy-template/custom_nodes/{repo.split('/')[1]} checkout {revision} && "
        f"if test -f /opt/comfy-template/custom_nodes/{repo.split('/')[1]}/requirements.txt; then "
        f"python -m pip install -r /opt/comfy-template/custom_nodes/{repo.split('/')[1]}/requirements.txt; fi"
        for repo, revision in NODE_REVISIONS.items()
    ])
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
        "names=[\"torch\",\"torchvision\",\"torchaudio\",\"xformers\",\"flash-attn\",\"sageattention\",\"comfyui-frontend-package\",\"comfyui-manager\",\"comfy-kitchen\"]; "
        "Path(\"/opt/split-constraints.txt\").write_text(\"\\n\".join(n+\"==\"+m.version(n) for n in names)+\"\\n\")'",
    )
    .env({"SPLIT_APP": APP_NAME, "SPLIT_VOLUMES": json.dumps(VOLUME_NAMES),
          "COMFYUI_SAGE_ATTENTION": "on" if SAGE_ATTENTION_ENABLED else "off",
          "SPLIT_GENERATION_TIMEOUT": str(FUNCTION_TIMEOUT),
          "PYTHONPATH": "/opt/split"})
    .add_local_file("comfyapp.py", "/root/comfyapp.py", copy=True)
    .add_local_dir("extensions/ComfyUI-Modal-Control", "/opt/comfy-extensions/ComfyUI-Modal-Control",
                   copy=True, ignore=["**/__pycache__/**", "**/*.pyc"])
    .add_local_dir("extensions/ComfyUI-Modal-Bridge", "/opt/comfy-extensions/ComfyUI-Modal-Bridge",
                   copy=True, ignore=["**/__pycache__/**", "**/*.pyc"])
    .add_local_dir("comfy_split", "/opt/split/comfy_split", copy=True,
                   ignore=["**/__pycache__/**", "**/*.pyc"])
    .run_commands("python -m comfy_split.check_environment --requirements /opt/comfy-template/requirements.txt")
)
app = modal.App(APP_NAME)


@app.function(image=image, gpu=str(GPU_PROFILE["modal_gpu"]),
              min_containers=0, max_containers=1, scaledown_window=30,
              timeout=86400, retries=0,
              volumes={MOUNTS[key]: value for key, value in volumes.items() if key != "state"})
async def gpu_worker(spec):
    from comfy_split.worker import run_worker
    return await run_worker(spec, events, commands, volumes)


@app.function(image=image, min_containers=0, max_containers=1, scaledown_window=30, cpu=2, memory=8192,
              timeout=86400, volumes={MOUNTS[key]: value for key, value in volumes.items()})
@modal.concurrent(max_inputs=100)
@modal.web_server(8000, startup_timeout=600, requires_proxy_auth=True)
def ui():
    gateway = subprocess.Popen(["python", "-m", "comfy_split.gateway"], env=dict(os.environ))
    
    def monitor():
        time.sleep(10)
        while True:
            if gateway.poll() is not None:
                print(f"Gateway process exited with code {gateway.returncode}, terminating container",
                      file=sys.stderr, flush=True)
                os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
    
    threading.Thread(target=monitor, daemon=True).start()
