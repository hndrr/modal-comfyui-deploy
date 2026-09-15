"""Deploy alongside splitapp.py: ./scripts/modal.sh deploy ambient_app.py."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import modal
import comfyapp  # Reuse this repo's dotenv resolution, GPU profile and Volume definitions.
from ambient.config import FASTVIDEO_REF, FAST_MODEL, MODEL_ROOT, FAST_MODEL_REVISION
from ambient.maintenance import cleanup_jobs
from ambient.processing import run_job
from ambient.readiness import check_comfyui, describe_modes
from ambient.service import JobService
from ambient.storage import AmbientStorage

app = modal.App("comfyui-ambient")
BASE = Path(__file__).parent
COMFY_URL = os.environ.get("AMBIENT_COMFYUI_URL", "").rstrip("/")
MODEL_REVISION = os.environ.get("AMBIENT_FASTH3_MODEL_REVISION", "")
if MODEL_REVISION and (
    len(MODEL_REVISION) != 40 or any(c not in "0123456789abcdef" for c in MODEL_REVISION)
):
    raise ValueError("AMBIENT_FASTH3_MODEL_REVISION must be a full Hugging Face commit SHA")

cpu_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi==0.115.14",
        "starlette==0.46.2",
        "python-multipart==0.0.22",
        "aiohttp==3.12.15",
        "pillow==11.3.0",
        "huggingface_hub==0.34.4",
        "python-dotenv==1.1.1",
    )
    .add_local_dir(BASE / "ambient", remote_path="/root/ambient")
)

# Separate dependencies: FastVideo cannot replace the shared ComfyUI's torch stack.
fast_image = (
    modal.Image.from_registry("nvidia/cuda:13.0.0-devel-ubuntu24.04", add_python="3.12")
    .apt_install("git", "ffmpeg", "build-essential", "libgl1", "libglib2.0-0")
    .pip_install("uv", "python-dotenv==1.1.1")
    .run_commands(
        f"git clone https://github.com/hao-ai-lab/FastVideo.git /opt/FastVideo && cd /opt/FastVideo && git checkout {FASTVIDEO_REF}",
        'cd /opt/FastVideo && UV_TORCH_BACKEND=cu130 uv pip install --system --no-sources-package fastvideo-kernel -e ".[fasth3]"',
    )
    .env(
        {
            "PYTHONPATH": "/opt/FastVideo:/root",
            "FASTVIDEO_ATTENTION_BACKEND": "VIDEO_SPARSE_ATTN_H3",
        }
    )
    .add_local_dir(BASE / "ambient", remote_path="/root/ambient")
)

configuration = modal.Secret.from_dict(
    {
        "AMBIENT_COMFYUI_URL": COMFY_URL,
        "MODAL_PROXY_KEY": os.environ.get("MODAL_PROXY_KEY", ""),
        "MODAL_PROXY_SECRET": os.environ.get("MODAL_PROXY_SECRET", ""),
        "AMBIENT_FASTH3_MODEL_REVISION": MODEL_REVISION,
    }
)


def store():
    return modal.Dict.from_name("comfyui-ambient-jobs", create_if_missing=True)


def is_cancelled(job_id):
    return bool(store().get("cancel:" + job_id))


def storage():
    return AmbientStorage(comfyapp.input_volume, comfyapp.output_volume)


@app.cls(
    image=fast_image,
    gpu=str(comfyapp.GPU_PROFILE["modal_gpu"]),
    memory=196608,
    timeout=comfyapp.FUNCTION_TIMEOUT,
    scaledown_window=comfyapp.SCALEDOWN_WINDOW,
    min_containers=0,
    max_containers=1,
    volumes={"/models": comfyapp.volume},
    secrets=[configuration],
)
class FastH3:
    @modal.enter()
    def load(self):
        from ambient.fasth3 import FastH3Engine

        self.engine = FastH3Engine(os.environ["AMBIENT_FASTH3_MODEL_REVISION"])

    @modal.method()
    def generate(self, request):
        if is_cancelled(request["requestId"]):
            return None
        return self.engine.generate(request)

    @modal.exit()
    def unload(self):
        if hasattr(self, "engine"):
            self.engine.close()


def generate_h3(request, image, source, cancelled, progress):
    from ambient import comfy

    headers = {}
    key, secret = os.environ.get("MODAL_PROXY_KEY"), os.environ.get("MODAL_PROXY_SECRET")
    if bool(key) != bool(secret):
        raise ValueError("ComfyUI proxy auth is incomplete")
    if key:
        headers = {"Modal-Key": key, "Modal-Secret": secret}
    asyncio.run(
        comfy.generate(
            os.environ["AMBIENT_COMFYUI_URL"],
            headers,
            request,
            image,
            source,
            cancelled,
            progress,
            max(60, comfyapp.FUNCTION_TIMEOUT - 180),
        )
    )


def generate_fasth3(request, image, source, cancelled, progress):
    if cancelled():
        return
    progress("FastH3 sampling")
    call = FastH3().generate.spawn(request)
    store().put("fast-call:" + request["requestId"], call.object_id)
    while not cancelled():
        try:
            result = call.get(timeout=5)
        except modal.exception.FunctionTimeoutError:
            # A worker timeout is terminal; it is not a poll with no result yet.
            raise
        except modal.exception.TimeoutError:
            continue
        if result is not None and not cancelled():
            source.write_bytes(result)
        return
    call.cancel()


@app.function(
    image=cpu_image,
    timeout=comfyapp.FUNCTION_TIMEOUT,
    max_containers=1,
    volumes={"/inputs": comfyapp.input_volume, "/outputs": comfyapp.output_volume},
    secrets=[configuration],
)
def process_job(job_id: str):
    run_job(job_id, store(), storage(), {"h3": generate_h3, "fasth3": generate_fasth3})


@app.function(image=cpu_image, min_containers=0, max_containers=1, secrets=[configuration])
@modal.concurrent(max_inputs=10)
@modal.asgi_app(requires_proxy_auth=True)
def api():
    from ambient.api import create_api

    def modes():
        return describe_modes(store(), COMFY_URL, MODEL_REVISION)

    def reconcile(call_id):
        try:
            modal.FunctionCall.from_id(call_id).get(timeout=0)
            return "Worker exited before publishing its result; inspect Modal logs."
        except modal.exception.TimeoutError:
            return None
        except Exception as error:
            return f"Worker terminated: {type(error).__name__}"

    service = JobService(
        store(), lambda job_id: process_job.spawn(job_id).object_id, reconcile=reconcile
    )
    return create_api(service, modes, comfyapp.input_volume, comfyapp.output_volume)


@app.function(
    image=cpu_image,
    timeout=86400,
    max_containers=1,
    volumes={"/models": comfyapp.volume},
    secrets=[modal.Secret.from_name("huggingface-secret")],
)
def prepare_fasth3(revision: str = FAST_MODEL_REVISION):
    from huggingface_hub import snapshot_download

    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("Pass a full Hugging Face commit SHA")
    target = Path(MODEL_ROOT) / revision
    snapshot_download(FAST_MODEL, revision=revision, local_dir=target)
    comfyapp.volume.commit()
    record = {
        "model": FAST_MODEL,
        "revision": revision,
        "fastvideo": FASTVIDEO_REF,
        "gpuValidated": False,
    }
    store().put("prepared:fasth3", record)
    return record


@app.function(
    image=cpu_image,
    schedule=modal.Period(hours=6),
    volumes={"/inputs": comfyapp.input_volume, "/outputs": comfyapp.output_volume},
)
def cleanup():
    cleanup_jobs(store(), storage())


@app.function(image=cpu_image, timeout=300, secrets=[configuration])
def check_h3():
    """Read splitapp's CPU node/model inventory without invoking its GPU worker."""
    headers = {
        "Modal-Key": os.environ.get("MODAL_PROXY_KEY", ""),
        "Modal-Secret": os.environ.get("MODAL_PROXY_SECRET", ""),
    }
    record = asyncio.run(check_comfyui(os.environ["AMBIENT_COMFYUI_URL"], headers))
    store().put("prepared:h3", record)
    return record
