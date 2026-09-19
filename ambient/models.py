"""Source references and explicit preparation manifests; importing never downloads."""

from .config import (
    COMFYUI_REFERENCE,
    COMFY_FAST_MODEL,
    COMFY_FAST_MODEL_REVISION,
    H3_MODEL_REVISION,
)

MODEL_FILES = {
    "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
}
FAST_MODEL_FILES = {
    **{key: value for key, value in MODEL_FILES.items() if key != "lora"},
    "unet": "minimax_h3_fastvideo_vsa_datafree_1300step_4step_int8_convrot.safetensors",
    "video_vae": "minimax_h3_video_vae_int8_convrot.safetensors",
}
FAST_CHECKSUMS = {
    "unet": "7221ae65d78780354d51e5048d29728d9f1f8fb9baf50b1dd3df85f5101413d3",
    "video_vae": "9bb2d96f218c76babd85e0611b85ca8fb330a90546c01a0005e8a58a59593410",
}


def comfy_assets(mode: str) -> list[dict]:
    """Arguments for the existing model saver, shared with reference reporting."""
    if mode not in ("h3", "fasth3"):
        raise ValueError("Invalid ComfyUI generation mode")
    files = MODEL_FILES if mode == "h3" else FAST_MODEL_FILES
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
        fast = mode == "fasth3" and key in FAST_CHECKSUMS
        asset = {
            "repo_id": COMFY_FAST_MODEL if fast else "Comfy-Org/MiniMax-H3",
            "revision": COMFY_FAST_MODEL_REVISION if fast else H3_MODEL_REVISION,
            "filename": files[key] if fast else f"{subdir}/{files[key]}",
            "destination_subdir": subdir,
        }
        if fast:
            asset["expected_sha256"] = FAST_CHECKSUMS[key]
        result.append(asset)
    return result


def references(mode: str, backend: str) -> dict:
    """Expected source references, not a claim that a GPU has validated these assets."""
    if backend != "comfyui":
        raise ValueError("Unsupported generation backend")
    return {
        "implementation": COMFYUI_REFERENCE,
        "recipe": "h3-turbo-8step" if mode == "h3" else "fasth3-vsa-4step",
        "models": comfy_assets(mode),
    }
