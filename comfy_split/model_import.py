"""CPU-only Modal function; importable without the ComfyUI deployment module."""

from pathlib import Path

from comfy_split.model_files import download
from comfy_split.storage import MOUNTS, VOLUME_NAMES


def import_model(**spec):
    import modal

    volume = modal.Volume.from_name(VOLUME_NAMES["models"])
    volume.reload()
    result = download(Path(MOUNTS["models"]), **spec)
    volume.commit()
    return result
