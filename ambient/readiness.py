"""Preparation records and native inventory checks, separate from GPU validation."""

import time
from itertools import product

from .h3 import workflow
from .contracts import ASPECT_RATIOS, DEFAULT_BACKENDS, H3_FOUR_STEP_MODES, IMAGE_MODES, RESOLUTIONS
from .models import references
from .config import COMFYUI_REFERENCE
from .split import SPLIT_HEADERS, check_dependencies, check_split
from .urls import redirect_guard, validate_endpoint


def describe_modes(jobs, comfy_url: str) -> dict:
    """Read saved preparation results without contacting ComfyUI."""
    modes = {}
    for mode in DEFAULT_BACKENDS:
        record = jobs.get(f"prepared:{mode}:comfyui")
        legacy = record is None and mode == "h3"
        if legacy:
            record = jobs.get("prepared:h3")
        record = record or {}
        expected = references(mode, "comfyui")
        ready = (
            bool(comfy_url)
            and record.get("url") == comfy_url
            and record.get("backend") == "split"
            and (legacy or record.get("references") == expected)
        )
        comfy = {
            "ready": ready,
            "reason": None
            if ready
            else (
                "Set AMBIENT_COMFYUI_URL to splitapp, prepare models, then run "
                f"check_comfy --mode {mode}"
            ),
            "validation": record,
        }
        backends = {"comfyui": comfy}
        default = DEFAULT_BACKENDS[mode]
        modes[mode] = {
            **backends[default],
            "defaultBackend": default,
            "backends": backends,
            "imageInput": mode in IMAGE_MODES,
            "requiresImage": mode == "fasth3-8step-i2v",
            "experimental": mode == "fasth3-8step-i2v",
            "camera": mode in IMAGE_MODES,
            "continuity": mode in IMAGE_MODES,
            "audio": True,
            "steps": 4 if mode in {"fasth3", *H3_FOUR_STEP_MODES} else 8,
        }
    return modes


async def check_comfyui(url: str, headers: dict, mode: str = "h3") -> dict:
    """Read the split CPU gateway's inventory without starting a GPU worker."""
    import aiohttp

    expected = references(mode, "comfyui")
    if not url:
        raise ValueError("Set AMBIENT_COMFYUI_URL before deployment")
    url = validate_endpoint(url, allow_http_loopback=not headers)
    async with aiohttp.ClientSession(
        headers={**headers, **SPLIT_HEADERS},
        timeout=aiohttp.ClientTimeout(total=240),
        trace_configs=[redirect_guard()],
    ) as client:
        state = await check_split(client, url)
        check_dependencies(state, mode)
        async with client.get(url + "/object_info") as response:
            response.raise_for_status()
            info = await response.json()
        validate_object_info(info, mode)
        async with client.get(url + "/system_stats") as response:
            response.raise_for_status()
            stats = await response.json()
    return {
        "url": url,
        "backend": "split",
        "environment": state.get("environment"),
        "comfyVersion": stats.get("system", {}).get("comfyui_version"),
        "workflowReference": COMFYUI_REFERENCE,
        "checkedAt": time.time(),
        "gpuValidated": False,
        "dependencies": state.get("dependencies", {}),
        "references": expected,
    }


def validate_object_info(info, mode="h3"):
    """Bind each supported recipe against the live catalog; execution stays upstream."""
    if mode not in DEFAULT_BACKENDS:
        raise ValueError("Invalid ComfyUI generation mode")
    for resolution, aspect in product(RESOLUTIONS, ASPECT_RATIOS):
        images = (("ambient/anchor.png",) if mode == "fasth3-8step-i2v"
                  else (None, "ambient/anchor.png") if mode in IMAGE_MODES else (None,))
        for image in images:
            workflow(
                {
                    "requestId": "00000000-0000-4000-8000-000000000001",
                    "mode": mode,
                    "prompt": "test",
                    "sound": "test",
                    "seed": 1,
                    "resolution": resolution,
                    "aspectRatio": aspect,
                },
                image,
                object_info=info,
            )
    return True
