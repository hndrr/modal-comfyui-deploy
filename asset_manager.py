"""Safe CRUD operations for ComfyUI assets stored in Modal Volumes."""

from __future__ import annotations

import mimetypes
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

import modal

from preserve_model import COMFY_MODEL_SUBDIRS

MODEL_VOLUME = "comfy-model"
INPUT_VOLUME = "comfy-inputs"
OUTPUT_VOLUME = "comfy-outputs"
ALLOWED_VOLUMES = frozenset({MODEL_VOLUME, INPUT_VOLUME, OUTPUT_VOLUME})
TRANSFERABLE_VOLUMES = frozenset({INPUT_VOLUME, OUTPUT_VOLUME})

IMAGE_EXTENSIONS = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
VIDEO_EXTENSIONS = frozenset({".m4v", ".mov", ".mp4", ".webm"})
AUDIO_EXTENSIONS = frozenset({".flac", ".m4a", ".mp3", ".ogg", ".wav"})


@dataclass(frozen=True)
class AssetEntry:
    volume: str
    path: str
    name: str
    kind: str
    size: int
    modified_at: datetime
    media_type: str

    @property
    def is_directory(self) -> bool:
        return self.kind == "directory"


def normalize_volume_path(raw_path: str | PurePosixPath, *, allow_root: bool = True) -> str:
    """Return a safe path relative to a Modal Volume root."""

    raw = str(raw_path).strip()
    if "\\" in raw:
        raise ValueError("Volume paths must use forward slashes.")
    if "\x00" in raw:
        raise ValueError("Volume paths cannot contain NUL bytes.")
    if raw in {"", "."}:
        if allow_root:
            return ""
        raise ValueError("The Volume root cannot be used for this operation.")

    path = PurePosixPath(raw)
    if path.is_absolute():
        raise ValueError("Volume paths must be relative.")
    if ".." in path.parts:
        raise ValueError("Volume paths cannot contain '..'.")

    normalized = path.as_posix()
    if normalized in {"", "."}:
        if allow_root:
            return ""
        raise ValueError("The Volume root cannot be used for this operation.")
    return normalized


def validate_volume(volume: str) -> str:
    if volume not in ALLOWED_VOLUMES:
        allowed = ", ".join(sorted(ALLOWED_VOLUMES))
        raise ValueError(f"Unsupported Volume {volume!r}. Allowed Volumes: {allowed}")
    return volume


def validate_model_destination(path: str) -> None:
    normalized = normalize_volume_path(path, allow_root=False)
    if PurePosixPath(normalized).parts[0] not in COMFY_MODEL_SUBDIRS:
        allowed = ", ".join(sorted(COMFY_MODEL_SUBDIRS))
        raise ValueError(f"Model destinations must be under one of: {allowed}")


def classify_media(path: str, kind: str) -> str:
    if kind == "directory":
        return "directory"
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    guessed_type, _ = mimetypes.guess_type(path)
    return guessed_type or "file"


def _entry_kind(entry_type: object) -> str:
    name = getattr(entry_type, "name", "")
    value = getattr(entry_type, "value", entry_type)
    if name == "DIRECTORY" or value == 2:
        return "directory"
    if name == "SYMLINK" or value == 3:
        return "symlink"
    return "file"


def _normalize_entry_path(path: str) -> str:
    return normalize_volume_path(path.lstrip("/"), allow_root=False)


def _to_asset_entry(volume: str, entry: object) -> AssetEntry:
    path = _normalize_entry_path(str(getattr(entry, "path")))
    kind = _entry_kind(getattr(entry, "type"))
    timestamp = int(getattr(entry, "mtime", 0))
    return AssetEntry(
        volume=volume,
        path=path,
        name=PurePosixPath(path).name,
        kind=kind,
        size=int(getattr(entry, "size", 0)),
        modified_at=datetime.fromtimestamp(timestamp, tz=timezone.utc),
        media_type=classify_media(path, kind),
    )


def _default_cross_volume_mover(
    source_volume: str,
    source_path: str,
    destination_volume: str,
    destination_path: str,
    overwrite: bool,
) -> None:
    """Move an entry between the input and output Volumes inside Modal."""

    app = modal.App(name="comfy-asset-manager-mover")
    source = modal.Volume.from_name(source_volume)
    destination = modal.Volume.from_name(destination_volume)
    source_mount = Path("/source")
    destination_mount = Path("/destination")

    @app.function(
        volumes={source_mount.as_posix(): source, destination_mount.as_posix(): destination},
        timeout=1800,
        serialized=True,
    )
    def move_between_volumes() -> None:
        source_absolute = source_mount / source_path
        destination_absolute = destination_mount / destination_path
        if not source_absolute.exists():
            raise FileNotFoundError(f"Source asset does not exist: {source_path}")
        if destination_absolute.exists():
            if not overwrite:
                raise FileExistsError(f"Destination already exists: {destination_path}")
            if destination_absolute.is_dir():
                shutil.rmtree(destination_absolute)
            else:
                destination_absolute.unlink()
        destination_absolute.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source_absolute.as_posix(), destination_absolute.as_posix())

    with app.run():
        move_between_volumes.remote()


class AssetManager:
    """Modal Volume-backed asset service with validation at every boundary."""

    def __init__(
        self,
        volume_factory: Callable[[str], object] | None = None,
        cross_volume_mover: Callable[[str, str, str, str, bool], None] | None = None,
    ) -> None:
        self._volume_factory = volume_factory or (
            lambda name: modal.Volume.from_name(name)
        )
        self._cross_volume_mover = cross_volume_mover or _default_cross_volume_mover

    def _volume(self, name: str):
        return self._volume_factory(validate_volume(name))

    def _find_entry(self, volume: str, path: str) -> AssetEntry:
        normalized = normalize_volume_path(path, allow_root=False)
        parent = PurePosixPath(normalized).parent.as_posix()
        entries = self._volume(volume).listdir(
            "/" if parent == "." else parent,
            recursive=False,
        )
        for entry in entries:
            asset = _to_asset_entry(volume, entry)
            if asset.path == normalized:
                return asset
        raise FileNotFoundError(f"Asset does not exist: {volume}:{normalized}")

    def list_assets(self, volume: str, path: str = "") -> list[AssetEntry]:
        normalized = normalize_volume_path(path, allow_root=True)
        query_path = normalized or "/"
        entries = self._volume(volume).listdir(query_path, recursive=False)
        return [_to_asset_entry(validate_volume(volume), entry) for entry in entries]

    def download_asset(self, volume: str, path: str, destination: str | Path) -> Path:
        normalized = normalize_volume_path(path, allow_root=False)
        entry = self._find_entry(volume, normalized)
        return self.download_listed_asset(entry, destination)

    def download_listed_asset(
        self,
        entry: AssetEntry,
        destination: str | Path,
    ) -> Path:
        """Download an entry already returned by ``list_assets`` without relisting."""

        validate_volume(entry.volume)
        normalized = normalize_volume_path(entry.path, allow_root=False)
        if entry.is_directory:
            raise IsADirectoryError("Directories cannot be downloaded as a single asset.")

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with destination_path.open("wb") as file_obj:
            self._volume(entry.volume).read_file_into_fileobj(normalized, file_obj)
        return destination_path

    def upload_assets(
        self,
        volume: str,
        destination: str,
        files: Sequence[str | Path],
        overwrite: bool = False,
    ) -> list[str]:
        validate_volume(volume)
        destination_dir = normalize_volume_path(destination, allow_root=True)
        local_files = [Path(file) for file in files]
        if not local_files:
            raise ValueError("Select at least one file to upload.")

        remote_paths: list[str] = []
        for local_file in local_files:
            if not local_file.is_file():
                raise FileNotFoundError(f"Local file does not exist: {local_file}")
            remote_path = PurePosixPath(destination_dir, local_file.name).as_posix()
            remote_path = normalize_volume_path(remote_path, allow_root=False)
            if volume == MODEL_VOLUME:
                validate_model_destination(remote_path)
            remote_paths.append(remote_path)

        target_volume = self._volume(volume)
        with target_volume.batch_upload(force=overwrite) as batch:
            for local_file, remote_path in zip(local_files, remote_paths, strict=True):
                batch.put_file(local_file, remote_path)
        return remote_paths

    def move_asset(
        self,
        source_volume: str,
        source_path: str,
        destination_volume: str,
        destination_path: str,
        overwrite: bool = False,
    ) -> str:
        validate_volume(source_volume)
        validate_volume(destination_volume)
        source = normalize_volume_path(source_path, allow_root=False)
        destination = normalize_volume_path(destination_path, allow_root=False)
        if source_volume == destination_volume and source == destination:
            raise ValueError("Source and destination are the same.")
        if destination_volume == MODEL_VOLUME:
            validate_model_destination(destination)
        if source_volume != destination_volume and {
            source_volume,
            destination_volume,
        } != TRANSFERABLE_VOLUMES:
            raise ValueError("Cross-Volume moves are only allowed between inputs and outputs.")

        source_entry = self._find_entry(source_volume, source)
        if source_entry.is_directory and destination.startswith(f"{source}/"):
            raise ValueError("A directory cannot be moved inside itself.")
        if source_volume != destination_volume:
            self._cross_volume_mover(
                source_volume,
                source,
                destination_volume,
                destination,
                overwrite,
            )
            return destination

        volume = self._volume(source_volume)
        destination_exists = False
        try:
            self._find_entry(destination_volume, destination)
            destination_exists = True
        except FileNotFoundError:
            pass
        if destination_exists and not overwrite:
            raise FileExistsError(f"Destination already exists: {destination}")
        if destination_exists:
            destination_entry = self._find_entry(destination_volume, destination)
            volume.remove_file(destination, recursive=destination_entry.is_directory)

        volume.copy_files([source], destination, recursive=source_entry.is_directory)
        volume.remove_file(source, recursive=source_entry.is_directory)
        return destination

    def delete_asset(self, volume: str, path: str, recursive: bool = False) -> None:
        normalized = normalize_volume_path(path, allow_root=False)
        entry = self._find_entry(volume, normalized)
        if entry.is_directory and not recursive:
            raise IsADirectoryError("Deleting a directory requires recursive=True.")
        self._volume(volume).remove_file(normalized, recursive=entry.is_directory)


_DEFAULT_MANAGER = AssetManager()


def list_assets(volume: str, path: str = "") -> list[AssetEntry]:
    return _DEFAULT_MANAGER.list_assets(volume, path)


def download_asset(volume: str, path: str, destination: str | Path) -> Path:
    return _DEFAULT_MANAGER.download_asset(volume, path, destination)


def upload_assets(
    volume: str,
    destination: str,
    files: Sequence[str | Path],
    overwrite: bool = False,
) -> list[str]:
    return _DEFAULT_MANAGER.upload_assets(volume, destination, files, overwrite)


def move_asset(
    source_volume: str,
    source_path: str,
    destination_volume: str,
    destination_path: str,
    overwrite: bool = False,
) -> str:
    return _DEFAULT_MANAGER.move_asset(
        source_volume,
        source_path,
        destination_volume,
        destination_path,
        overwrite,
    )


def delete_asset(volume: str, path: str, recursive: bool = False) -> None:
    _DEFAULT_MANAGER.delete_asset(volume, path, recursive)
