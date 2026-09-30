# Bundled ComfyUI node

`sol_attn_minimax_v5.py` is the unmodified experimental node published by Kijai in [Comfy-Org/comfy-kitchen PR #117](https://github.com/Comfy-Org/comfy-kitchen/pull/117).

Source: <https://github.com/user-attachments/files/31576773/sol_attn_minimax_v5.py>

SHA-256: `97c9d56fdc7c9a102e59bff9ac8d79503299514d061892088a03d99dcf415b0c`

The Split image includes it in `/opt/comfy-extensions`, on both CPU and GPU, without modifying the user's saved custom-node environment. It uses the already pinned comfy-kitchen dependency. No model files are bundled or downloaded. The four-step VSA recipe remains experimental.

## Ambient extension

The optional extension is maintained in the private [ComfyUI-Ambient repository](https://github.com/hndrr/ComfyUI-Ambient). It is not vendored here. `splitapp.py` installs the Git commit pinned in `comfy_split/extension_sources.py` during the image build when `SPLIT_EXTENSIONS=ambient`. The same commit is pinned in `pyproject.toml` and `uv.lock` for local tests and explicit model preparation.
