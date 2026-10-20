"""Disposable Function/Server deployments with optional read-only shared weights.

SPLIT_EVALUATION_ID and SPLIT_EVALUATION_BACKEND are mandatory. The runner
selects this module's `app` explicitly; importing the shared image definition
does not deploy splitapp.app. All runtime lookups use the isolated names below.
"""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import modal
from comfy_split.config import Settings
from comfy_split.evaluation import Evaluation
from comfy_split.storage import MOUNTS, VOLUME_NAMES

evaluation = Evaluation.read(os.environ)
# The comparison deliberately excludes optional integrations and their secrets.
os.environ.update(Settings().environment())
os.environ["SPLIT_CPU_MEMORY_SNAPSHOT"] = "0"
from splitapp import image as shared_image  # noqa: E402

app = modal.App(evaluation.app_name, tags={"evaluation": evaluation.run_id,
                                         "backend": evaluation.backend})
shared_models = os.environ.get("SPLIT_EVALUATION_SHARED_MODELS") == "1"
volume_names = {**evaluation.volumes, **({"models": VOLUME_NAMES["models"]} if shared_models else {})}
volumes = {key: (modal.Volume.from_name(name).with_mount_options(read_only=True)
                 if shared_models and key == "models" else modal.Volume.from_name(name, create_if_missing=True))
           for key, name in volume_names.items()}
events = modal.Queue.from_name(evaluation.app_name + "-events", create_if_missing=True)
commands = modal.Queue.from_name(evaluation.app_name + "-commands", create_if_missing=True)
image = shared_image.add_local_file("splitapp.py", "/root/splitapp.py", copy=True).env({
    **Settings().environment(),
    "SPLIT_APP": evaluation.app_name,
    "SPLIT_VOLUMES": json.dumps(volume_names),
    "SPLIT_UI_BACKEND": evaluation.backend,
    "SPLIT_EVALUATION_ID": evaluation.run_id,
    "SPLIT_EVALUATION_BACKEND": evaluation.backend,
    "SPLIT_EVALUATION_SHARED_MODELS": "1" if shared_models else "0",
    "SPLIT_CPU_MEMORY_SNAPSHOT": "0",
    "SPLIT_GENERATION_TIMEOUT": "600",
})
mounts = {MOUNTS[key]: value for key, value in volumes.items()}


@app.function(image=image, gpu="RTX-PRO-6000", min_containers=0, max_containers=1,
              scaledown_window=30, timeout=900, retries=0, volumes=mounts)
async def gpu_worker(spec):
    from comfy_split.worker import run_worker

    return await run_worker(spec, events, commands, volumes)


def start_gateway():
    from comfy_split.cpu_snapshot import resume

    gateway = resume()

    def monitor():
        while gateway.poll() is None:
            time.sleep(2)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=monitor, daemon=True).start()
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if gateway.poll() is not None:
            raise RuntimeError("Evaluation gateway exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", 8000), timeout=1):
                return gateway
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("Evaluation gateway did not listen on port 8000")


cpu_image = image.env({"SPLIT_EVALUATION_PRELOAD": "1"})
# Same snapshot boundary as the existing Function: preload ComfyUI at import,
# restore the durable controller only after Modal has refreshed its credentials.
if not modal.is_local() and os.environ.get("SPLIT_EVALUATION_PRELOAD") == "1":
    from comfy_split.cpu_snapshot import prepare

    prepare()

cpu_resources = dict(image=cpu_image, cpu=2, memory=8192, min_containers=0,
                     max_containers=1, scaledown_window=30, startup_timeout=600,
                     enable_memory_snapshot=True, volumes=mounts)

if evaluation.backend == "function":
    @app.function(**cpu_resources, timeout=3600)
    @modal.concurrent(max_inputs=100)
    @modal.asgi_app(requires_proxy_auth=True)
    def ui():
        from comfy_split.modal_proxy import web_server_proxy

        start_gateway()
        return web_server_proxy("127.0.0.1", 8000)
else:
    @app.server(**cpu_resources, name="ui", port=8000, max_concurrency=100,
                unauthenticated=False, exit_grace_period=30)
    class UI:
        @modal.enter()
        def start(self):
            self.gateway = start_gateway()

        @modal.exit()
        def stop(self):
            if getattr(self, "gateway", None) and self.gateway.poll() is None:
                self.gateway.terminate()
                try:
                    self.gateway.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.gateway.kill()
