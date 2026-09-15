from __future__ import annotations

import hashlib
import json
from uuid import UUID

RESOLUTIONS = {"preview": (832, 480), "quality": (1344, 768)}
FRAMES = 124
FPS = 24
TERMINAL = {"completed", "failed", "cancelled"}
DEFAULT_BACKENDS = {"h3": "comfyui", "fasth3": "comfyui"}
ROUTES = (("h3", "comfyui"), ("fasth3", "comfyui"))


def stored_backend(request: dict) -> str:
    """Backend-less saved FastH3 jobs predate ComfyUI support; never reinterpret them."""
    return request.get("backend", "fastvideo" if request["mode"] == "fasth3" else "comfyui")


def identifier(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a UUID")
    parsed = str(UUID(value))
    if parsed != value.lower():
        raise ValueError("Expected a canonical UUID")
    return parsed


def validate_request(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    allowed = {"requestId", "mode", "backend", "prompt", "sound", "seed", "resolution", "imageId", "parentClipId"}
    if set(data) - allowed:
        raise ValueError("Unknown request fields")
    out = {"requestId": identifier(data.get("requestId"))}
    for key, limit in (("prompt", 8000), ("sound", 4000)):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{key} must contain 1–{limit} characters")
        out[key] = value.strip()
    if data.get("mode") not in ("h3", "fasth3"):
        raise ValueError("Invalid generation mode")
    backend = data.get("backend", DEFAULT_BACKENDS[data["mode"]])
    if (data["mode"], backend) not in ROUTES:
        raise ValueError("Unsupported generation mode/backend combination")
    if not isinstance(data.get("resolution"), str) or data.get("resolution") not in RESOLUTIONS:
        raise ValueError("Invalid resolution")
    seed = data.get("seed")
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("Seed must be an integer from 0 to 2147483647")
    out.update(mode=data["mode"], backend=backend, resolution=data["resolution"], seed=seed)
    for key in ("imageId", "parentClipId"):
        if data.get(key) is not None:
            out[key] = identifier(data[key])
    if "imageId" in out and "parentClipId" in out:
        raise ValueError("Choose imageId or parentClipId")
    if out["mode"] == "fasth3" and ("imageId" in out or "parentClipId" in out):
        raise ValueError("FastH3 Preview supports text-to-video-and-audio only")
    return out


def fingerprint(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


def prompt_text(request: dict) -> str:
    return (f"integrated_multimodal_description: [Shot 1] {request['prompt']}\n\n"
            f"overall_soundscape: {request['sound']}\n\nnon_diegetic_music: N/A")


def public_job(job: dict, cancelled: bool = False) -> dict:
    result = {k: job[k] for k in ("id", "status", "stage", "error", "clip", "references") if k in job}
    if "request" in job:
        result.update(mode=job["request"]["mode"], backend=stored_backend(job["request"]))
    if cancelled:
        result.update(status="cancelled", stage="Cancelled")
        result.pop("clip", None)
    return result
