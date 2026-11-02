"""Model operations restricted to one regular file in a known loader category."""

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

CATEGORIES = frozenset(
    "checkpoints diffusion_models loras text_encoders audio_encoders clip clip_vision controlnet vae embeddings latent_upscale_models upscale_models detection".split()
)
WEIGHTS = {".safetensors", ".gguf", ".onnx", ".bin", ".pt", ".pth", ".ckpt"}


def checked(value, msg):
    if not value:
        raise ValueError(msg)


def components(s):
    checked(isinstance(s, str) and len(s) <= 512, "Invalid model path")
    parts = s.split("/")
    checked(
        all(
            p
            and not p.startswith(".")
            and not any(c in "\\:" or ord(c) < 32 or ord(c) == 127 for c in p)
            for p in parts
        ),
        "Invalid model path",
    )
    return parts


def target(root, name):
    parts = components(name)
    checked(
        len(parts) >= 2 and parts[0] in CATEGORIES and Path(name).suffix.lower() in WEIGHTS,
        "Unsupported model category or extension",
    )
    p = root
    for part in parts:
        p = p / part
        checked(not p.is_symlink(), "Symlinked model paths are not supported")
    return p


def inspect(root, name):
    p = target(root, name)
    st = p.lstat()
    checked(stat.S_ISREG(st.st_mode), "Expected one regular model file")
    token = hashlib.sha256(
        f"{name}:{st.st_size}:{st.st_mtime_ns}:{st.st_ino}:{st.st_ctime_ns}".encode()
    ).hexdigest()
    return {"path": name, "size_bytes": st.st_size, "version": token}


def trash(root, operation):
    checked(re.fullmatch(r"[A-Za-z0-9_-]{1,96}", operation), "Invalid removal ID")
    base = root / ".split-mcp-trash"
    checked(not base.is_symlink(), "Invalid model trash")
    base.mkdir(exist_ok=True)
    return base / operation


def remove(root, name, version, operation):
    p = target(root, name)
    dest = trash(root, operation)
    checked(inspect(root, name)["version"] == version, "Model changed; inspect it again")
    checked(not os.path.lexists(dest), "Removal ID already used")
    p.rename(dest)
    return {"path": name, "removal_id": operation, "status": "removed"}


def restore(root, name, operation):
    p = target(root, name)
    src = trash(root, operation)
    checked(not src.is_symlink() and src.is_file(), "Removed file not found")
    reserve(p, src)
    return {"path": name, "status": "restored"}


def reserve(p, src):
    # Volume supports symlinks and replace but not hardlink/rename-noreplace.
    p.symlink_to(os.path.relpath(src, p.parent))
    try:
        os.replace(src, p)
    except OSError:
        p.unlink(missing_ok=True)
        raise


def import_spec(repo, file, category, revision="main", sha256=None):
    checked(
        isinstance(repo, str)
        and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*", repo)
        and ".." not in repo,
        "Expected a Hugging Face owner/repository",
    )
    parts = components(file)
    checked(
        category in CATEGORIES and Path(file).suffix.lower() in {".safetensors", ".gguf", ".onnx"},
        "Use a safetensors, GGUF or ONNX model in a supported category",
    )
    checked(
        isinstance(revision, str)
        and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._/-]{0,119}", revision)
        and ".." not in revision,
        "Invalid repository revision",
    )
    checked(sha256 is None or re.fullmatch("[0-9a-f]{64}", sha256), "Invalid SHA-256")
    return category + "/" + parts[-1]


def download(root, repo, file, category, revision="main", sha256=None):
    from huggingface_hub import HfApi, hf_hub_download

    path = import_spec(repo, file, category, revision, sha256)
    dest = target(root, path)
    checked(not os.path.lexists(dest), "Model filename already in use")
    dest.parent.mkdir(exist_ok=True)
    commit = HfApi().model_info(repo, revision=revision).sha
    checked(
        isinstance(commit, str) and re.fullmatch("[0-9a-f]{40}", commit), "No immutable source revision"
    )
    stage = root / ".split-mcp-downloads"
    checked(not stage.is_symlink(), "Invalid model staging directory")
    stage.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=stage) as directory:
        src = Path(hf_hub_download(repo, file, revision=commit, local_dir=directory))
        with src.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        checked(sha256 is None or digest == sha256, "Model checksum mismatch")
        reserve(dest, src)
    return {**inspect(root, path), "revision": commit, "sha256": digest}
