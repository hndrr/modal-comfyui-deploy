"""Split CPU GUIs: scripts/modal.sh deploy -m comfy_split.split_guis."""
import os
import subprocess
import sys
from urllib.parse import urlsplit

import modal

from preserve_model import download_app, gui_image

APP_NAME = "comfy-split-guis"
app = modal.App(APP_NAME)
app.include(download_app)

# Modal can replace forwarded headers. Give Gradio the canonical public URL so
# its queue, generated links and file requests always come back through Access.
def public_origin(value: str) -> str:
    error = "Set SPLIT_GUI_PUBLIC_ORIGIN to the split HTTPS origin before deploying."
    try:
        url = urlsplit(value)
        port = url.port  # Also validates an explicit port.
    except ValueError:
        raise ValueError(error) from None
    if (url.scheme != "https" or not url.hostname or url.username is not None or port == 0
            or url.path not in ("", "/") or any(c in value for c in "\\?#")
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError(error)
    return "https://" + url.netloc


public_url = os.environ.get("SPLIT_GUI_PUBLIC_ORIGIN", "")
if modal.is_local():
    public_url = public_origin(public_url)
models_image = gui_image({
    "PRESERVE_MODEL_DEPLOYED_APP": APP_NAME,
    "SPLIT_GUI_PUBLIC_ORIGIN": public_url,
    "GRADIO_ANALYTICS_ENABLED": "False",
})


@app.function(image=models_image, min_containers=0, max_containers=1, scaledown_window=30, timeout=1800)
@modal.concurrent(max_inputs=20)
@modal.asgi_app(requires_proxy_auth=True)
def models():
    from fastapi import FastAPI
    from gradio import mount_gradio_app
    from preserve_model_gui import build_interface

    blocks = build_interface(show_standalone_options=False)
    blocks.queue()
    return mount_gradio_app(FastAPI(), blocks, path="/", root_path=public_origin(public_url) + "/models/_ui")


asset_image = (
    modal.Image.from_registry("node:24.22.2-bookworm-slim", add_python="3.12")
    .apt_install("ffmpeg")
    .pip_install("modal==1.1.4", "pillow>=13,<14")
    .env({"ASSET_GUI_BASE": "/assets/_ui/", "COMFY_ASSET_MOUNT_ROOT": "/volumes"})
    .workdir("/opt/gui/web")
)
for filename in ("package.json", "package-lock.json"):
    asset_image = asset_image.add_local_file("web/" + filename, "/opt/gui/web/" + filename, copy=True)
asset_image = asset_image.run_commands("npm ci --no-audit --no-fund")
for filename in ("index.html", "vite.config.ts", "tsconfig.server.json", "tsconfig.json"):
    asset_image = asset_image.add_local_file("web/" + filename, "/opt/gui/web/" + filename, copy=True)
for dirname in ("src", "server"):
    asset_image = asset_image.add_local_dir("web/" + dirname, "/opt/gui/web/" + dirname, copy=True)
asset_image = asset_image.run_commands("npm run build", "npm prune --omit=dev --no-audit").env({
    "NODE_ENV": "production",
    # Both Modal's Python entrypoint and the Node child import these shared modules.
    "PYTHONPATH": "/opt/gui",
})
for filename in ("asset_rpc.py", "asset_manager.py", "preserve_model.py"):
    asset_image = asset_image.add_local_file(filename, "/opt/gui/" + filename)
asset_volumes = {"/volumes/" + name: modal.Volume.from_name(name)
                 for name in ("comfy-inputs", "comfy-outputs", "comfy-model")}


@app.function(image=asset_image, volumes=asset_volumes, min_containers=0, max_containers=1,
              scaledown_window=30, timeout=1800)
@modal.concurrent(max_inputs=32)
@modal.web_server(7860, startup_timeout=45, requires_proxy_auth=True)
def assets():
    subprocess.Popen(["node", "server-dist/modal.js"], cwd="/opt/gui/web",
                     env={**os.environ, "COMFY_ASSET_PYTHON": sys.executable})
