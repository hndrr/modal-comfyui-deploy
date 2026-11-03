"""Saved-media delivery only: scripts/modal.sh deploy mediaapp.py.

Separate app and image so deploying this endpoint never rebuilds ComfyUI.
Proxy Auth is mandatory. Only output and job-data Volumes are attached.
"""

import modal

from comfy_split.storage import MOUNTS, VOLUME_NAMES

app = modal.App("comfyui-split-media")
image = modal.Image.debian_slim(python_version="3.12").add_local_python_source("comfy_split")
volumes = {key: modal.Volume.from_name(VOLUME_NAMES[key]) for key in ("output", "data")}


@app.function(image=image, cpu=0.25, memory=512, min_containers=0, max_containers=1,
              scaledown_window=30, timeout=900,
              volumes={MOUNTS[key]: volume for key, volume in volumes.items()})
@modal.concurrent(max_inputs=16)
@modal.asgi_app(requires_proxy_auth=True)
def serve():
    from comfy_split.media_server import MediaServer

    return MediaServer(MOUNTS["output"], MOUNTS["data"], tuple(volumes.values()))
