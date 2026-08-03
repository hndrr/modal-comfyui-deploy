from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from asset_manager import (
    INPUT_VOLUME,
    MODEL_VOLUME,
    OUTPUT_VOLUME,
    AssetManager,
    classify_media,
    normalize_volume_path,
)


class FakeBatch:
    def __init__(self, volume, force: bool) -> None:
        self.volume = volume
        self.force = force

    def __enter__(self):
        self.volume.batch_forces.append(self.force)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def put_file(self, local_path, remote_path) -> None:
        self.volume.uploads.append((Path(local_path), str(remote_path)))


class FakeVolume:
    def __init__(self, entries=None, contents=None) -> None:
        self.entries = dict(entries or {})
        self.contents = dict(contents or {})
        self.uploads = []
        self.batch_forces = []
        self.copies = []
        self.removals = []
        self.copy_error = None

    def listdir(self, path: str, recursive: bool = False):
        if path == "/":
            return list(self.entries.values())
        entry = self.entries.get(path)
        return [entry] if entry else []

    def read_file_into_fileobj(self, path: str, file_obj) -> None:
        file_obj.write(self.contents[path])

    def batch_upload(self, force: bool = False):
        return FakeBatch(self, force)

    def copy_files(self, source_paths, destination, recursive=False) -> None:
        self.copies.append((list(source_paths), destination, recursive))
        if self.copy_error:
            raise self.copy_error

    def remove_file(self, path: str, recursive: bool = False) -> None:
        self.removals.append((path, recursive))


def entry(path: str, kind: int = 1, size: int = 10, mtime: int = 1_700_000_000):
    return SimpleNamespace(path=path, type=kind, size=size, mtime=mtime)


class AssetManagerValidationTests(unittest.TestCase):
    def test_normalizes_safe_paths_and_root(self) -> None:
        self.assertEqual(normalize_volume_path("foo//bar"), "foo/bar")
        self.assertEqual(normalize_volume_path(""), "")
        with self.assertRaises(ValueError):
            normalize_volume_path("", allow_root=False)
        for invalid in ("/absolute", "../escape", "foo/../escape", "foo\\bar"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_volume_path(invalid)

    def test_media_classification(self) -> None:
        self.assertEqual(classify_media("images/result.PNG", "file"), "image")
        self.assertEqual(classify_media("movie.mp4", "file"), "video")
        self.assertEqual(classify_media("sound.wav", "file"), "audio")
        self.assertEqual(classify_media("folder", "directory"), "directory")

    def test_rejects_unapproved_volume_and_root_delete(self) -> None:
        manager = AssetManager(volume_factory=lambda _: FakeVolume())
        with self.assertRaises(ValueError):
            manager.list_assets("unrelated-volume")
        with self.assertRaises(ValueError):
            manager.delete_asset(INPUT_VOLUME, "")


class AssetManagerOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.input = FakeVolume(
            entries={
                "images/a.png": entry("images/a.png", size=3),
                "folder": entry("folder", kind=2),
            },
            contents={"images/a.png": b"png"},
        )
        self.output = FakeVolume()
        self.model = FakeVolume()
        self.volumes = {
            INPUT_VOLUME: self.input,
            OUTPUT_VOLUME: self.output,
            MODEL_VOLUME: self.model,
        }
        self.cross_moves = []
        self.manager = AssetManager(
            volume_factory=self.volumes.__getitem__,
            cross_volume_mover=lambda *args: self.cross_moves.append(args),
        )

    def test_lists_and_downloads_file(self) -> None:
        assets = self.manager.list_assets(INPUT_VOLUME)
        self.assertEqual([asset.path for asset in assets], ["images/a.png", "folder"])
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "a.png"
            self.manager.download_asset(INPUT_VOLUME, "images/a.png", destination)
            self.assertEqual(destination.read_bytes(), b"png")

    def test_rejects_directory_download_and_requires_recursive_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(IsADirectoryError):
                self.manager.download_asset(INPUT_VOLUME, "folder", Path(temp_dir) / "x")
        with self.assertRaises(IsADirectoryError):
            self.manager.delete_asset(INPUT_VOLUME, "folder")
        self.manager.delete_asset(INPUT_VOLUME, "folder", recursive=True)
        self.assertEqual(self.input.removals, [("folder", True)])

    def test_uploads_multiple_files_and_passes_overwrite_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.png"
            second = Path(temp_dir) / "second.txt"
            first.write_bytes(b"1")
            second.write_bytes(b"2")
            uploaded = self.manager.upload_assets(
                INPUT_VOLUME,
                "incoming",
                [first, second],
                overwrite=True,
            )
        self.assertEqual(uploaded, ["incoming/first.png", "incoming/second.txt"])
        self.assertEqual(self.input.batch_forces, [True])

    def test_model_upload_destination_is_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            model = Path(temp_dir) / "model.safetensors"
            model.write_bytes(b"model")
            with self.assertRaises(ValueError):
                self.manager.upload_assets(MODEL_VOLUME, "misc", [model])
            uploaded = self.manager.upload_assets(MODEL_VOLUME, "loras", [model])
        self.assertEqual(uploaded, ["loras/model.safetensors"])

    def test_same_volume_move_copies_before_deleting_source(self) -> None:
        moved = self.manager.move_asset(
            INPUT_VOLUME,
            "images/a.png",
            INPUT_VOLUME,
            "archive/a.png",
        )
        self.assertEqual(moved, "archive/a.png")
        self.assertEqual(self.input.copies, [(["images/a.png"], "archive/a.png", False)])
        self.assertEqual(self.input.removals, [("images/a.png", False)])

    def test_copy_failure_keeps_source(self) -> None:
        self.input.copy_error = RuntimeError("copy failed")
        with self.assertRaises(RuntimeError):
            self.manager.move_asset(
                INPUT_VOLUME,
                "images/a.png",
                INPUT_VOLUME,
                "archive/a.png",
            )
        self.assertEqual(self.input.removals, [])

    def test_cross_volume_move_is_limited_to_inputs_and_outputs(self) -> None:
        self.manager.move_asset(
            INPUT_VOLUME,
            "images/a.png",
            OUTPUT_VOLUME,
            "archive/a.png",
            overwrite=True,
        )
        self.assertEqual(
            self.cross_moves,
            [(INPUT_VOLUME, "images/a.png", OUTPUT_VOLUME, "archive/a.png", True)],
        )
        with self.assertRaises(ValueError):
            self.manager.move_asset(
                INPUT_VOLUME,
                "images/a.png",
                MODEL_VOLUME,
                "loras/a.png",
            )

    def test_rejects_move_to_existing_destination_without_overwrite(self) -> None:
        self.input.entries["archive/a.png"] = entry("archive/a.png")
        with self.assertRaises(FileExistsError):
            self.manager.move_asset(
                INPUT_VOLUME,
                "images/a.png",
                INPUT_VOLUME,
                "archive/a.png",
            )
        self.assertEqual(self.input.copies, [])

    def test_rejects_moving_directory_inside_itself(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.move_asset(
                INPUT_VOLUME,
                "folder",
                INPUT_VOLUME,
                "folder/nested",
            )


if __name__ == "__main__":
    unittest.main()
