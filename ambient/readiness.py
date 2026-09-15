"""Preparation records and native inventory checks, separate from GPU validation."""

import time

from .h3 import workflow
from .contracts import RESOLUTIONS
from .config import COMFYUI_REFERENCE
from .urls import redirect_guard, validate_endpoint


def describe_modes(jobs, comfy_url: str, model_revision: str) -> dict:
    """Read saved preparation results without contacting either generation backend."""
    h3_record = jobs.get("prepared:h3") or {}
    fast_record = jobs.get("prepared:fasth3") or {}
    h3 = bool(comfy_url) and h3_record.get("url") == comfy_url
    fast = bool(model_revision) and fast_record.get("revision") == model_revision
    return {
        "h3": {
            "ready": h3,
            "imageInput": True,
            "camera": True,
            "continuity": True,
            "audio": True,
            "steps": 8,
            "reason": None
            if h3
            else "Set AMBIENT_COMFYUI_URL, prepare H3 models, then run check_h3",
            "validation": h3_record,
        },
        "fasth3": {
            "ready": fast,
            "imageInput": False,
            "camera": False,
            "continuity": False,
            "audio": True,
            "steps": 4,
            "reason": None if fast else "Run prepare_fasth3 with the pinned revision and redeploy",
            "validation": fast_record,
        },
    }


async def check_comfyui(url: str, headers: dict) -> dict:
    """Contact ComfyUI explicitly; this can wake it, but never submits a prompt."""
    import aiohttp

    if not url:
        raise ValueError("Set AMBIENT_COMFYUI_URL before deployment")
    url = validate_endpoint(url)
    async with aiohttp.ClientSession(
        headers=headers, timeout=aiohttp.ClientTimeout(total=240), trace_configs=[redirect_guard()]
    ) as client:
        async with client.get(url + "/object_info") as response:
            response.raise_for_status()
            info = await response.json()
        validate_object_info(info)
        async with client.get(url + "/system_stats") as response:
            response.raise_for_status()
            stats = await response.json()
    return {
        "url": url,
        "comfyVersion": stats.get("system", {}).get("comfyui_version"),
        "workflowReference": COMFYUI_REFERENCE,
        "checkedAt": time.time(),
        "gpuValidated": False,
    }


def validate_object_info(info):
    """Bind each supported recipe against the live catalog; execution stays upstream."""
    for resolution in RESOLUTIONS:
        for image in (None, "ambient/anchor.png"):
            workflow(
                {
                    "requestId": "00000000-0000-4000-8000-000000000001",
                    "prompt": "test",
                    "sound": "test",
                    "seed": 1,
                    "resolution": resolution,
                },
                image,
                object_info=info,
            )
    return True
