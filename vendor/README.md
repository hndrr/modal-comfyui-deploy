# Bundled ComfyUI node

`sol_attn_minimax_v5.py` is the unmodified experimental node published by Kijai in [Comfy-Org/comfy-kitchen PR #117](https://github.com/Comfy-Org/comfy-kitchen/pull/117).

Source: https://github.com/user-attachments/files/31576773/sol_attn_minimax_v5.py

SHA-256: `97c9d56fdc7c9a102e59bff9ac8d79503299514d061892088a03d99dcf415b0c`

The Split image includes it in `/opt/comfy-extensions`, on both CPU and GPU, without modifying the user's saved custom-node environment. It uses the already pinned comfy-kitchen dependency. No model files are bundled or downloaded. The four-step VSA recipe remains experimental.

## Ambient Studio shared package

`ambient_comfyui-0.1.0-py3-none-any.whl` is built from Ambient Studio's `extensions/ComfyUI-Ambient`. `ambient-comfyui.json` records the version, source directory and SHA-256; image construction verifies the digest before adding it. It includes shared contracts, Local/Modal model profiles, a split adapter and the frontend. Standard split images do not include or import it.

Build with `python3 scripts/package-ambient-python.py --output /path/to/modal-comfyui-deploy/vendor` in Ambient Studio, then update `uv.lock` and run both repositories' tests. Do not edit the wheel by hand. Builds are deterministic; the Python distribution and the native ComfyUI ZIP use the same sources. Bump the package version for subsequent releases and update the pinned wheel path and dependency together.
