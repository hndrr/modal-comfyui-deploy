"""Deploy the split architecture separately: scripts/modal.sh deploy splitapp.py."""

import json
import os
import signal
import shlex
import sys
import threading
import time
import uuid

import modal

from comfy_split import node_packs
from comfy_split.config import Settings, DEPLOYMENT_ENV
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
settings = Settings.read()
# One identity per deployment, shared by CPU snapshots and node refreshes.
DEPLOYMENT_ID = os.environ.get(DEPLOYMENT_ENV) or uuid.uuid4().hex
provider_settings = settings.secrets(os.environ)
secret_names = {name: value for name, _, value in provider_settings}
provider_secrets = [modal.Secret.from_name(value, required_keys=[key])
                    for _, key, value in provider_settings]
github_secrets = []
github_build_secret = None
if settings.node_packs or "ambient" in settings.extensions:
    secret_names["GITHUB_SECRET_NAME"] = os.environ.get("GITHUB_SECRET_NAME", "").strip() or "github-secret"
    github_build_secret = modal.Secret.from_name(secret_names["GITHUB_SECRET_NAME"],
                                                required_keys=[node_packs.TOKEN_ENV])
    if settings.node_packs:
        github_secrets = [github_build_secret]
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
            **settings.environment(),
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
    .add_local_file(
        "vendor/sol_attn_minimax_v5.py",
        "/opt/comfy-extensions/sol_attn_minimax_v5.py",
        copy=True,
    )
    .add_local_dir(
        "comfy_split",
        "/opt/split/comfy_split",
        copy=True,
        ignore=["**/__pycache__/**", "**/*.pyc"],
    )
    .run_commands(
        "python -m comfy_split.check_environment --requirements /opt/comfy-template/requirements.txt"
    )
)
if "ambient" in settings.extensions:
    from comfy_split.extension_sources import AMBIENT

    frontend_init = 'NODE_CLASS_MAPPINGS = {}\nWEB_DIRECTORY = "./web"\n'
    frontend_setup = (
        'import ambient_comfyui, shutil; from pathlib import Path; '
        'p=Path("/opt/comfy-extensions/ComfyUI-Ambient"); p.mkdir(); '
        'shutil.copytree(Path(ambient_comfyui.__file__).parent/"web",p/"web"); '
        f'p.joinpath("__init__.py").write_text({frontend_init!r})'
    )
    image = image.pip_install_private_repos(
        f"github.com/{AMBIENT['repository']}@{AMBIENT['revision']}",
        git_user="x-access-token", secrets=[github_build_secret], extra_options="--no-deps",
    ).run_commands(
        "python -c " + shlex.quote(
            "from importlib.metadata import version; "
            f"assert version('ambient-comfyui') == {AMBIENT['version']!r}"
        ), "python -c " + shlex.quote(frontend_setup),
    )
# Deployment-only layer last, so source and dependency builds remain cached.
image = image.env({DEPLOYMENT_ENV: DEPLOYMENT_ID})
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
    secrets=provider_secrets,
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
    secrets=[*provider_secrets, *github_secrets],
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
