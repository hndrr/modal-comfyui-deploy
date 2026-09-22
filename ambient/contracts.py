from __future__ import annotations

import hashlib
import json
import math
from uuid import UUID

RESOLUTIONS = {"preview": (832, 480), "quality": (1344, 768)}
# Exact ratios on H3's 32-pixel grid.
ASPECT_RATIOS = {
    "16:9": {"preview": (1024, 576), "quality": (1536, 864)},
    "9:16": {"preview": (576, 1024), "quality": (864, 1536)},
    "1:1": {"preview": (640, 640), "quality": (1024, 1024)},
    "4:3": {"preview": (768, 576), "quality": (1152, 864)},
    "3:4": {"preview": (576, 768), "quality": (864, 1152)},
}
FRAMES = 124
FPS = 24
TERMINAL = {"completed", "failed", "cancelled"}
FAST8_MODES = {"fasth3-8step-t2v", "fasth3-8step-i2v"}
H3_FOUR_STEP_MODES = {"h3-turbo-4step", "h3-fused-4step"}
IMAGE_MODES = {"h3", *H3_FOUR_STEP_MODES, "fasth3-8step-i2v"}
DEFAULT_BACKENDS = {mode: "comfyui" for mode in (
    "h3", "h3-turbo-4step", "h3-fused-4step", "fasth3", "fasth3-8step-t2v", "fasth3-8step-i2v",
)}
ROUTES = tuple(DEFAULT_BACKENDS.items())


def generation_size(request: dict) -> tuple[int, int]:
    # Requests accepted before aspect-ratio support keep their original dimensions.
    sizes = ASPECT_RATIOS[request["aspectRatio"]] if "aspectRatio" in request else RESOLUTIONS
    return sizes[request["resolution"]]


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
    allowed = {"requestId", "mode", "backend", "prompt", "sound", "seed", "resolution", "aspectRatio", "imageId", "parentClipId", "saveToLibrary", "sessionId", "workflowRevision", "intent", "generationContext"}
    if set(data) - allowed:
        raise ValueError("Unknown request fields")
    out = {"requestId": identifier(data.get("requestId"))}
    for key, limit in (("prompt", 8000), ("sound", 4000)):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{key} must contain 1–{limit} characters")
        out[key] = value.strip()
    if not isinstance(data.get("mode"), str) or data["mode"] not in DEFAULT_BACKENDS:
        raise ValueError("Invalid generation mode")
    backend = data.get("backend", DEFAULT_BACKENDS[data["mode"]])
    if (data["mode"], backend) not in ROUTES:
        raise ValueError("Unsupported generation mode/backend combination")
    if not isinstance(data.get("resolution"), str) or data.get("resolution") not in RESOLUTIONS:
        raise ValueError("Invalid resolution")
    if "aspectRatio" in data:
        ratio = data["aspectRatio"]
        if not isinstance(ratio, str) or ratio not in ASPECT_RATIOS:
            raise ValueError("Invalid aspect ratio")
        out["aspectRatio"] = ratio
    seed = data.get("seed")
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("Seed must be an integer from 0 to 2147483647")
    out.update(mode=data["mode"], backend=backend, resolution=data["resolution"], seed=seed)
    for key in ("imageId", "parentClipId"):
        if data.get(key) is not None:
            out[key] = identifier(data[key])
    if "imageId" in out and "parentClipId" in out:
        raise ValueError("Choose imageId or parentClipId")
    if out["mode"] not in IMAGE_MODES and ("imageId" in out or "parentClipId" in out):
        raise ValueError("FastH3 Preview supports text-to-video-and-audio only")
    if out["mode"] == "fasth3-8step-i2v" and not (out.get("imageId") or out.get("parentClipId") or data.get("sessionId")):
        raise ValueError("FastH3 8-step I2V requires a first-frame image or parent clip")
    if "saveToLibrary" in data:
        if type(data["saveToLibrary"]) is not bool:
            raise ValueError("saveToLibrary must be boolean")
        out["saveToLibrary"] = data["saveToLibrary"]
    if "generationContext" in data:
        out["generationContext"] = validate_generation_context(data["generationContext"])
    if "intent" in data:
        intent = data["intent"]
        if not isinstance(intent, dict) or set(intent) != {"prompt", "sound"}:
            raise ValueError("intent must contain prompt and sound")
        for key, limit in (("prompt", 8000), ("sound", 4000)):
            if not isinstance(intent[key], str) or len(intent[key]) > limit:
                raise ValueError(f"Invalid intent {key}")
        out["intent"] = dict(intent)
    if "sessionId" in data or "workflowRevision" in data:
        out["sessionId"] = identifier(data.get("sessionId"))
        revision = data.get("workflowRevision")
        if type(revision) is not int or revision < 0:
            raise ValueError("workflowRevision must be a nonnegative integer")
        out["workflowRevision"] = revision
    return out


def validate_generation_context(value):
    """Optional client selection metadata; absent on legacy idempotent requests."""
    allowed = {"version", "scenePreset", "soundPresets", "lookPreset", "family", "features", "source"}
    if not isinstance(value, dict) or set(value) != allowed or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Invalid generationContext")
    def label(v):
        return isinstance(v, str) and 0 < len(v) <= 80
    for key in ("scenePreset", "lookPreset", "family"):
        if value[key] is not None and not label(value[key]):
            raise ValueError(f"Invalid generationContext {key}")
    sounds = value["soundPresets"]
    if not isinstance(sounds, list) or len(sounds) > 4 or not all(label(v) for v in sounds):
        raise ValueError("Invalid generationContext soundPresets")
    if value["source"] not in ("current", "manual", "nearby", "explore"):
        raise ValueError("Invalid generationContext source")
    features = value["features"]
    if not isinstance(features, dict) or set(features) - {"tags", "scores"}:
        raise ValueError("Invalid generationContext features")
    tags, scores = features.get("tags", []), features.get("scores", {})
    if not isinstance(tags, list) or len(tags) > 48 or not all(label(v) for v in tags):
        raise ValueError("Invalid generationContext tags")
    if not isinstance(scores, dict) or set(scores) - {"motion", "warmth", "dream", "space"}:
        raise ValueError("Invalid generationContext scores")
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values()):
        raise ValueError("Invalid generationContext score")
    return json.loads(json.dumps(value))


MUSIC_DIRECTION = "Follow overall_soundscape for music, rhythm, percussion and silence."

def fingerprint(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


def prompt_text(request: dict) -> str:
    return (f"integrated_multimodal_description: [Shot 1] {request['prompt']}\n\n"
            f"overall_soundscape: {request['sound']}\n\nnon_diegetic_music: {MUSIC_DIRECTION}")


def public_job(job: dict) -> dict:
    result = {k: job[k] for k in ("id", "status", "stage", "progress", "error", "clip", "references") if k in job}
    if "request" in job:
        result.update(mode=job["request"]["mode"], backend=stored_backend(job["request"]))
    if result["status"] == "cancelled":
        result.pop("clip", None)
    return result
