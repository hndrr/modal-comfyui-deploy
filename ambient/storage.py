"""Ambient-owned files on the existing input and output Volumes."""

import io
from pathlib import Path
from uuid import uuid4

from .contracts import RESOLUTIONS, identifier
from .media import finalize


def image_path(asset_id: str) -> str:
    return f"ambient/images/{identifier(asset_id)}.png"


def frame_path(clip_id: str) -> str:
    return f"ambient/frames/{identifier(clip_id)}.png"


def clip_path(clip_id: str) -> str:
    return f"ambient/clips/{identifier(clip_id)}.mp4"


class AmbientStorage:
    def __init__(self, inputs, outputs, input_root=Path("/inputs"), output_root=Path("/outputs")):
        self.inputs = inputs
        self.outputs = outputs
        self.input_root = Path(input_root)
        self.output_root = Path(output_root)

    def save_image(self, data: bytes) -> str:
        from PIL import Image, ImageOps

        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.width * source.height > 24_000_000:
                    raise ValueError("Image exceeds 24 megapixels")
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((1920, 1920))
                output = io.BytesIO()
                image.save(output, format="PNG")
        except (OSError, Image.DecompressionBombError) as error:
            raise ValueError("Invalid image") from error
        asset_id = str(uuid4())
        with self.inputs.batch_upload() as batch:
            batch.put_file(io.BytesIO(output.getvalue()), image_path(asset_id))
        return asset_id

    def prepare_anchor(self, request: dict, directory: Path) -> Path | None:
        from PIL import Image, ImageOps

        if request.get("imageId"):
            source = image_path(request["imageId"])
        elif request.get("parentClipId"):
            source = frame_path(request["parentClipId"])
        else:
            return None
        anchor = directory / "anchor.png"
        with anchor.open("wb") as handle:
            self.inputs.read_file_into_fileobj(source, handle)
        # H3 stretches its first-frame input; crop to the output aspect before upload.
        fitted = directory / "fitted.png"
        with Image.open(anchor) as incoming:
            ImageOps.fit(incoming.convert("RGB"), RESOLUTIONS[request["resolution"]]).save(fitted)
        return fitted

    def publish_clip(self, source: Path, job_id: str) -> dict:
        clip = finalize(
            source,
            self.output_root / clip_path(job_id),
            self.input_root / frame_path(job_id),
            job_id,
        )
        # The processor may publish completed only after both artifacts are durable.
        self.commit()
        return clip

    def download_clip(self, clip_id: str, target: Path) -> None:
        # SDK reads avoid mounted volume.reload races with active HTTP downloads.
        with target.open("wb") as handle:
            self.outputs.read_file_into_fileobj(clip_path(clip_id), handle)

    def remove_clip(self, job_id: str) -> None:
        clip = self.output_root / clip_path(job_id)
        clip.unlink(missing_ok=True)
        clip.with_suffix(".part.mp4").unlink(missing_ok=True)
        (self.input_root / frame_path(job_id)).unlink(missing_ok=True)

    def expire_temporary_files(self, cutoff: float) -> None:
        for root in (
            self.input_root / "ambient/images",
            self.input_root / "ambient/uploads",
            self.output_root / "ambient/raw",
        ):
            if root.exists():
                for path in root.rglob("*"):
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()

    def reload(self) -> None:
        self.inputs.reload()
        self.outputs.reload()

    def commit(self) -> None:
        self.inputs.commit()
        self.outputs.commit()
