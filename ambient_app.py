"""Deploy alongside splitapp.py: ./scripts/modal.sh deploy ambient_app.py."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import modal
import comfyapp  # Reuse this repo's dotenv resolution, timeout and Volume definitions.
from ambient.maintenance import cleanup_jobs
from ambient.processing import run_job
from ambient.readiness import check_comfyui, describe_modes
from ambient.service import JobService
from ambient.storage import AmbientStorage
from ambient.models import references

app = modal.App("comfyui-ambient")
BASE = Path(__file__).parent
COMFY_URL = os.environ.get("AMBIENT_COMFYUI_URL", "").rstrip("/")
cpu_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi==0.115.14",
        "starlette==0.46.2",
        "python-multipart==0.0.22",
        "aiohttp==3.12.15",
        "pillow==11.3.0",
        "python-dotenv==1.1.1",
    )
    .add_local_file(BASE / "comfyapp.py", remote_path="/root/comfyapp.py")
    .add_local_dir(BASE / "comfy_split", remote_path="/root/comfy_split",
                   ignore=["**/__pycache__/**", "**/*.pyc"])
    .add_local_dir(BASE / "ambient", remote_path="/root/ambient",
                   ignore=["docs/**", "**/__pycache__/**", "**/*.pyc"])
)

configuration = modal.Secret.from_dict(
    {
        "AMBIENT_COMFYUI_URL": COMFY_URL,
        "MODAL_PROXY_KEY": os.environ.get("MODAL_PROXY_KEY", ""),
        "MODAL_PROXY_SECRET": os.environ.get("MODAL_PROXY_SECRET", ""),
    }
)


def store():
    return modal.Dict.from_name("comfyui-ambient-jobs", create_if_missing=True)


def storage():
    return AmbientStorage(comfyapp.input_volume, comfyapp.output_volume)


def library():
    from ambient.library import Library
    return Library(modal.Dict.from_name("comfyui-ambient-library", create_if_missing=True), comfyapp.output_volume)


def generate_comfy(request, image, source, cancelled, progress):
    from ambient import comfy

    headers = {}
    key, secret = os.environ.get("MODAL_PROXY_KEY"), os.environ.get("MODAL_PROXY_SECRET")
    if bool(key) != bool(secret):
        raise ValueError("ComfyUI proxy auth is incomplete")
    if key:
        headers = {"Modal-Key": key, "Modal-Secret": secret}
    return asyncio.run(
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


@app.function(
    image=cpu_image,
    timeout=comfyapp.FUNCTION_TIMEOUT,
    max_containers=1,
    volumes={"/inputs": comfyapp.input_volume, "/outputs": comfyapp.output_volume},
    secrets=[configuration],
)
def process_job(job_id: str):
    run_job(job_id, store(), storage(), {
        ("h3", "comfyui"): generate_comfy,
        ("fasth3", "comfyui"): generate_comfy,
    }, library=library(), tag_dispatch=lambda clip_id, effective, request: tag_clip.spawn(clip_id, effective, request))


@app.function(image=cpu_image, timeout=1200, secrets=[configuration])
def tag_clip(clip_id: str, effective: dict, request: dict):
    from ambient.tagging import classify
    headers = {"Modal-Key": os.environ["MODAL_PROXY_KEY"], "Modal-Secret": os.environ["MODAL_PROXY_SECRET"]}
    try:
        result = asyncio.run(classify(COMFY_URL, headers, effective.get("effective", {
            "prompt": request["prompt"], "sound": request["sound"]}),
            request.get("sessionId", clip_id), request.get("workflowRevision", 0)))
    except Exception as error:
        result = {"status": "failed", "error": str(error)[:300]}
    try:
        library().tags(clip_id, result)
    except KeyError:
        pass  # Deleted while tagging was running.


@app.function(image=cpu_image, min_containers=0, max_containers=1, secrets=[configuration])
@modal.concurrent(max_inputs=10)
@modal.asgi_app(requires_proxy_auth=True)
def api():
    from ambient.api import create_api

    def modes():
        return describe_modes(store(), COMFY_URL)

    def reconcile(call_id):
        try:
            modal.FunctionCall.from_id(call_id).get(timeout=0)
            return "Worker exited before publishing its result; inspect Modal logs."
        except modal.exception.FunctionTimeoutError:
            return "Worker execution timed out; inspect Modal logs."
        except (TimeoutError, modal.exception.TimeoutError):
            return None
        except Exception as error:
            return f"Worker terminated: {type(error).__name__}"

    service = JobService(
        store(), lambda job_id: process_job.spawn(job_id).object_id, reconcile=reconcile,
        reference=lambda req: references(req["mode"], req["backend"]),
    )
    return create_api(service, modes, comfyapp.input_volume, comfyapp.output_volume, library=library())


@app.function(
    image=cpu_image,
    schedule=modal.Period(hours=6),
    volumes={"/inputs": comfyapp.input_volume, "/outputs": comfyapp.output_volume},
)
def cleanup():
    cleanup_jobs(store(), storage())


def check_comfy_mode(mode):
    """Read splitapp's CPU node/model inventory without invoking its GPU worker."""
    headers = {
        "Modal-Key": os.environ.get("MODAL_PROXY_KEY", ""),
        "Modal-Secret": os.environ.get("MODAL_PROXY_SECRET", ""),
    }
    record = asyncio.run(check_comfyui(os.environ["AMBIENT_COMFYUI_URL"], headers, mode))
    store().put(f"prepared:{mode}:comfyui", record)
    if mode == "h3":
        store().put("prepared:h3", record)
    return record


@app.function(image=cpu_image, timeout=300, secrets=[configuration])
def check_comfy(mode: str = "h3"):
    return check_comfy_mode(mode)


@app.function(image=cpu_image, timeout=300, secrets=[configuration])
def check_h3():
    """Compatibility entry point for the original CPU inventory check."""
    return check_comfy_mode("h3")
