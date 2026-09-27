"""Explicit model preparation manifests; importing never downloads."""
from ambient_comfyui.models import MODAL_MODE_MODEL_FILES

H3_MODEL_REVISION = 'a98869194787969724c7425d95d0ed73ce9202af'
COMFY_FAST_MODEL = 'Kijai/MiniMax-H3-experimental'
COMFY_FAST_MODEL_REVISION = 'f4cac997f880e93cf6940af61ee8d58ef31ff7f3'
COMFY_FAST8_MODEL = 'FastVideo/FastVideo-FastH3-Comfy'
COMFY_FAST8_MODEL_REVISION = '0de92ab26fcb74ee47596332d93e55d20cddfd45'
COMFY_FUSED_MODEL = 'MATLOWAI/minimax-h3-fused-turbo-int8-convrot'
COMFY_FUSED_MODEL_REVISION = '8a8dffaa0cd99c6184833ae0a3b4e9b0089c17b3'
PREPARATION_MODES = ("h3", "h3-turbo-4step", "h3-fused-4step", "fasth3", "fasth3-8step-t2v", "fasth3-8step-i2v")
DEFAULT_BACKENDS = PREPARATION_MODES
FAST8_MODES = {"fasth3-8step-t2v", "fasth3-8step-i2v"}

MODEL_FILES = MODAL_MODE_MODEL_FILES["h3"]
FAST_MODEL_FILES = MODAL_MODE_MODEL_FILES["fasth3"]
FAST8_MODEL_FILES = MODAL_MODE_MODEL_FILES["fasth3-8step-t2v"]
FUSED_MODEL_FILES = MODAL_MODE_MODEL_FILES["h3-fused-4step"]
MODE_MODEL_FILES = {mode: MODAL_MODE_MODEL_FILES[mode] for mode in DEFAULT_BACKENDS}
FAST_CHECKSUMS = {
    "unet": "7221ae65d78780354d51e5048d29728d9f1f8fb9baf50b1dd3df85f5101413d3",
    "video_vae": "9bb2d96f218c76babd85e0611b85ca8fb330a90546c01a0005e8a58a59593410",
}
FAST8_SHA256 = "0922785978dc9bfe1adf27d8b291b0ca763f9f165f882e6cb297c72fbb6deda8"
FUSED_SHA256 = "4262e4e9963c553fa00016bbe83961407a4fc0a888be95fd836c8d4f2304e48b"


def comfy_assets(mode: str) -> list[dict]:
    """Pinned arguments for the explicit model saver."""
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

