"""Source references and explicit preparation manifests; importing never downloads."""

from .config import (
    COMFYUI_REFERENCE,
    COMFY_FAST_MODEL,
    COMFY_FAST_MODEL_REVISION,
    COMFY_FAST8_MODEL,
    COMFY_FAST8_MODEL_REVISION,
    COMFY_FUSED_MODEL,
    COMFY_FUSED_MODEL_REVISION,
    H3_MODEL_REVISION,
)
from .contracts import DEFAULT_BACKENDS, FAST8_MODES

MODEL_FILES = {
    "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae": "minimax_h3_video_vae_int8_convrot.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
}
FAST_MODEL_FILES = {
    **{key: value for key, value in MODEL_FILES.items() if key != "lora"},
    "unet": "minimax_h3_fastvideo_vsa_datafree_1300step_4step_int8_convrot.safetensors",
}
FAST_CHECKSUMS = {
    "unet": "7221ae65d78780354d51e5048d29728d9f1f8fb9baf50b1dd3df85f5101413d3",
    "video_vae": "9bb2d96f218c76babd85e0611b85ca8fb330a90546c01a0005e8a58a59593410",
}
FAST8_MODEL_FILES = {
    **FAST_MODEL_FILES,
    "unet": "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors",
}
FAST8_SHA256 = "0922785978dc9bfe1adf27d8b291b0ca763f9f165f882e6cb297c72fbb6deda8"
FUSED_MODEL_FILES = {
    **FAST_MODEL_FILES,
    "unet": "minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors",
}
FUSED_SHA256 = "4262e4e9963c553fa00016bbe83961407a4fc0a888be95fd836c8d4f2304e48b"
MODE_MODEL_FILES = {
    "h3": MODEL_FILES,
    "h3-turbo-4step": MODEL_FILES,
    "h3-fused-4step": FUSED_MODEL_FILES,
    "fasth3": FAST_MODEL_FILES,
    **{mode: FAST8_MODEL_FILES for mode in FAST8_MODES},
}


def comfy_assets(mode: str) -> list[dict]:
    """Arguments for the existing model saver, shared with reference reporting."""
    if mode not in DEFAULT_BACKENDS:
        raise ValueError("Invalid ComfyUI generation mode")
    files = MODE_MODEL_FILES[mode]
    result = []
    for key, subdir in (
        ("unet", "diffusion_models"),
        ("clip", "text_encoders"),
        ("video_vae", "vae"),
        ("audio_vae", "vae"),
        ("lora", "loras"),
    ):
        if key not in files:
            continue
        fast = key in FAST_CHECKSUMS and files[key] == FAST_MODEL_FILES[key]
        asset = {
            "repo_id": COMFY_FAST_MODEL if fast else "Comfy-Org/MiniMax-H3",
            "revision": COMFY_FAST_MODEL_REVISION if fast else H3_MODEL_REVISION,
            "filename": files[key] if fast else f"{subdir}/{files[key]}",
            "destination_subdir": subdir,
        }
        if fast:
            asset["expected_sha256"] = FAST_CHECKSUMS[key]
        if mode in FAST8_MODES and key == "unet":
            asset.update(repo_id=COMFY_FAST8_MODEL, revision=COMFY_FAST8_MODEL_REVISION,
                         filename=f"{subdir}/{files[key]}", expected_sha256=FAST8_SHA256)
        if mode == "h3-fused-4step" and key == "unet":
            asset.update(repo_id=COMFY_FUSED_MODEL, revision=COMFY_FUSED_MODEL_REVISION,
                         filename=f"{subdir}/{files[key]}", expected_sha256=FUSED_SHA256)
        result.append(asset)
    return result


def references(mode: str, backend: str) -> dict:
    """Expected source references, not a claim that a GPU has validated these assets."""
    if backend != "comfyui":
        raise ValueError("Unsupported generation backend")
    return {
        "implementation": COMFYUI_REFERENCE,
        "recipe": {"h3": "h3-turbo-8step", "fasth3": "fasth3-vsa-4step",
                   "h3-turbo-4step": "h3-turbo-4step-res-multistep",
                   "h3-fused-4step": "h3-fused-mystic-4step-res-multistep-dense",
                   "fasth3-8step-t2v": "fasth3-v2-8step-vsa-t2v",
                   "fasth3-8step-i2v": "fasth3-v2-8step-sol-attn-i2v"}[mode],
        "models": comfy_assets(mode),
    }
