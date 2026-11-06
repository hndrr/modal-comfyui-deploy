"""Set SPLIT_COMFYUI_REVISION, then scripts/modal.sh run -m scripts.verify_split_gpus.

Uses billable GPUs sequentially. Select the deployed SHA to compare its revision.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import modal

from comfy_split.storage import VOLUME_NAMES
from comfy_split.release import REVISION_ENV

if modal.is_local():
    import comfyapp  # Load shared .env settings before checking the required SHA.

    if not os.environ.get(REVISION_ENV, "").strip():
        raise ValueError(f"Set {REVISION_ENV} to the deployed ComfyUI SHA before GPU verification")
    from splitapp import image
else:
    image = None  # The worker already runs the resolved split image.

app = modal.App("split-gpu-verification")
model_volume = modal.Volume.from_name(VOLUME_NAMES["models"]).read_only()
options = dict(image=image, volumes={"/models": model_volume}, timeout=900, max_containers=1,
               scaledown_window=2, retries=0)


def verify(graph, size):
    import requests
    import torch
    from flash_attn import flash_attn_func
    from PIL import Image
    from sageattention import sageattn

    revision = subprocess.check_output(
        ["git", "-C", "/opt/comfy-template", "rev-parse", "HEAD"], text=True).strip()
    q = torch.randn(1, 8, 128, 64, device="cuda", dtype=torch.float16)
    assert sageattn(q, q, q).isfinite().all()
    q = q.transpose(1, 2).contiguous()
    assert flash_attn_func(q, q, q).isfinite().all()
    name = torch.cuda.get_device_name()

    origin = "http://127.0.0.1:8188"
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "models").symlink_to("/models", target_is_directory=True)
        start = time.monotonic()
        process = subprocess.Popen([sys.executable, "main.py", "--base-directory", tmp,
            "--disable-all-custom-nodes", "--use-sage-attention", "--port", "8188"], cwd="/opt/comfy-template")
        try:
            while process.poll() is None and time.monotonic() - start < 120:
                try:
                    if requests.get(origin + "/system_stats", timeout=2).ok:
                        break
                except requests.ConnectionError:
                    pass
                time.sleep(1)
            graph["6"]["inputs"].update(width=size, height=size)
            response = requests.post(origin + "/prompt", json={"prompt": graph}, timeout=30)
            if not response.ok:
                raise RuntimeError(response.text[:1600])
            key = response.json()["prompt_id"]
            deadline = time.monotonic() + 360
            entry = None
            while time.monotonic() < deadline:
                entry = requests.get(origin + "/history/" + key, timeout=10).json().get(key)
                if entry:
                    break
                time.sleep(.5)
            if not entry or entry["status"]["status_str"] != "success":
                raise RuntimeError(str(entry and entry["status"])[-1800:])
            saved = entry["outputs"]["9"]["images"][0]
            with Image.open(root / "output" / saved["subfolder"] / saved["filename"]) as output:
                output.load()
                assert output.size == (size, size)
            return {"device": name, "size": size, "comfy_revision": revision,
                    "seconds": round(time.monotonic() - start, 2)}
        finally:
            process.terminate()
            process.wait(timeout=20)


@app.function(gpu="L4", **options)
def l4(graph, size):
    return verify(graph, size)


@app.function(gpu="L40S", **options)
def l40s(graph, size):
    return verify(graph, size)


@app.function(gpu="RTX-PRO-6000", **options)
def rtx(graph, size):
    return verify(graph, size)


@app.local_entrypoint()
def main(gpu: str = "all", size: int = 0):
    workers = {"L40S": l40s, "RTX-PRO-6000": rtx, "L4": l4}
    graph = json.loads(Path(__file__).with_name("verify_split_gpu_workflow.json").read_text())
    devices = workers if gpu == "all" else [gpu]
    for device in devices:
        worker = workers[device]
        for resolution in (512, 1024) if size == 0 else [size]:
            result = worker.remote(graph, resolution)
            print(json.dumps(result), flush=True)
            deadline = time.monotonic() + 150
            while worker.get_current_stats().num_total_runners:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Previous GPU is still running")
                time.sleep(2)
            print("GPU stopped:", result["device"], flush=True)
