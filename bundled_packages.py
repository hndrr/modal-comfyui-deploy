"""Verify optional, locally bundled distributions before building Modal images."""

import hashlib
import json
from pathlib import Path


def ambient_wheel():
    vendor = Path(__file__).resolve().parent / "vendor"
    if not vendor.is_dir():
        vendor = Path("/opt/wheels")  # Re-imported inside a built Modal image.
    manifest = json.loads((vendor / "ambient-comfyui.json").read_text())
    path = vendor / manifest["file"]
    if path.parent != vendor or hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Bundled Ambient extension checksum mismatch")
    return path
