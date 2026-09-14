"""Preparation records and native inventory checks, separate from GPU validation."""

import time

from .comfy import MODEL_FILES, workflow
from .config import COMFYUI_REFERENCE


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

    url = url.rstrip("/")
    if not url:
        raise ValueError("Set AMBIENT_COMFYUI_URL before deployment")
    async with aiohttp.ClientSession(
        headers=headers, timeout=aiohttp.ClientTimeout(total=240)
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
    required = {
        node["class_type"]
        for node in workflow(
            {
                "requestId": "00000000-0000-4000-8000-000000000001",
                "prompt": "test",
                "sound": "test",
                "seed": 1,
                "resolution": "preview",
            },
            "anchor.png",
        ).values()
    }
    missing = sorted(required - info.keys())
    if missing:
        raise ValueError("Update ComfyUI; missing native nodes: " + ", ".join(missing))

    def choices(node, field):
        fields = {
            **info[node]["input"].get("required", {}),
            **info[node]["input"].get("optional", {}),
        }
        spec = fields.get(field, [])
        return spec[0] if spec and isinstance(spec[0], list) else []

    for node, field, name in [
        ("UNETLoader", "unet_name", "unet"),
        ("CLIPLoader", "clip_name", "clip"),
        ("VAELoader", "vae_name", "video_vae"),
        ("VAELoader", "vae_name", "audio_vae"),
        ("LoraLoaderModelOnly", "lora_name", "lora"),
    ]:
        if MODEL_FILES[name] not in choices(node, field):
            raise ValueError("Missing model: " + MODEL_FILES[name])
    if "minimax" not in choices("CLIPLoader", "type"):
        raise ValueError("ComfyUI CLIPLoader has no minimax type")
    if "res_multistep" not in choices("KSamplerSelect", "sampler_name"):
        raise ValueError("ComfyUI has no res_multistep sampler")
    fields = info["MiniMaxH3ImageToVideo"]["input"]
    if "first_frame" not in {**fields.get("required", {}), **fields.get("optional", {})}:
        raise ValueError("H3 native node has no first_frame input")
    return True
