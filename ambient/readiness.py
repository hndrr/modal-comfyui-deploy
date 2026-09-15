"""Preparation records and native inventory checks, separate from GPU validation."""

import time

from .h3 import workflow
from .contracts import DEFAULT_BACKENDS, RESOLUTIONS
from .models import references
from .config import COMFYUI_REFERENCE
from .split import SPLIT_HEADERS, check_dependencies, check_split
from .urls import redirect_guard, validate_endpoint


def describe_modes(jobs, comfy_url: str, model_revision: str) -> dict:
    """Read saved preparation results without contacting either generation backend."""
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
        if mode == "fasth3":
            fast_record = jobs.get("prepared:fasth3:fastvideo")
            legacy_fast = fast_record is None
            if legacy_fast:
                fast_record = jobs.get("prepared:fasth3")
            fast_record = fast_record or {}
            fast_ready = (
                bool(model_revision) and fast_record.get("revision") == model_revision
            )
            if not legacy_fast:
                fast_ready = fast_ready and fast_record.get("references") == references(
                    mode, "fastvideo", model_revision
                )
            backends["fastvideo"] = {
                "ready": fast_ready,
                "reason": None
                if fast_ready
                else "Run prepare_fasth3 with the pinned revision and redeploy",
                "validation": fast_record,
            }
        default = DEFAULT_BACKENDS[mode]
        modes[mode] = {
            **backends[default],
            "defaultBackend": default,
            "backends": backends,
            "imageInput": mode == "h3",
            "camera": mode == "h3",
            "continuity": mode == "h3",
            "audio": True,
            "steps": 8 if mode == "h3" else 4,
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
    for resolution in RESOLUTIONS:
        for image in (None, "ambient/anchor.png") if mode == "h3" else (None,):
            workflow(
                {
                    "requestId": "00000000-0000-4000-8000-000000000001",
                    "mode": mode,
                    "prompt": "test",
                    "sound": "test",
                    "seed": 1,
                    "resolution": resolution,
                },
                image,
                object_info=info,
            )
    return True
