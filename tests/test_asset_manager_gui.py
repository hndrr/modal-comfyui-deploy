from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import asset_manager_gui
from asset_manager import INPUT_VOLUME, MODEL_VOLUME, AssetEntry
from preserve_model_gui import _parse_repo_and_filename


class FakeGuiManager:
    def __init__(self, entries):
        self.entries = entries
        self.downloaded = []
        self.deleted = []

    def list_assets(self, volume, path):
        return list(self.entries)

    def download_asset(self, volume, path, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"preview")
        self.downloaded.append((volume, path))
        return destination

    def delete_asset(self, volume, path, recursive=False):
        self.deleted.append((volume, path, recursive))


def asset(path: str, *, kind="file", size=10, media_type="file") -> AssetEntry:
    return AssetEntry(
        volume=INPUT_VOLUME,
        path=path,
        name=Path(path).name,
        kind=kind,
        size=size,
        modified_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        media_type=media_type,
    )


class BrowserViewTests(unittest.TestCase):
    def test_builds_gallery_table_filter_and_breadcrumb(self) -> None:
        manager = FakeGuiManager(
            [
                asset("folder", kind="directory", media_type="directory"),
                asset("result.png", media_type="image"),
                asset("notes.txt"),
            ]
        )
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(asset_manager_gui, "ASSET_MANAGER", manager):
                view = asset_manager_gui.build_browser_view(
                    INPUT_VOLUME,
                    "nested/path",
                    "",
                    "name_asc",
                    1,
                    workspace,
                )
        self.assertEqual(len(view.gallery), 1)
        self.assertEqual([row[1] for row in view.table], ["folder", "notes.txt"])
        self.assertEqual(view.breadcrumb_choices[-1], ("/ nested/path", "nested/path"))
        self.assertEqual(manager.downloaded, [(INPUT_VOLUME, "result.png")])

    def test_models_use_table_even_for_images(self) -> None:
        model_image = asset("checkpoints/cover.png", media_type="image")
        model_image = AssetEntry(**{**model_image.__dict__, "volume": MODEL_VOLUME})
        manager = FakeGuiManager([model_image])
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(asset_manager_gui, "ASSET_MANAGER", manager):
                view = asset_manager_gui.build_browser_view(
                    MODEL_VOLUME, "checkpoints", "", "name_asc", 1, workspace
                )
        self.assertEqual(view.gallery, [])
        self.assertEqual(view.table[0][1], "cover.png")
        self.assertEqual(manager.downloaded, [])

    def test_search_and_page_bounds(self) -> None:
        entries = [asset(f"keep-{index}.png", media_type="image") for index in range(30)]
        entries.append(asset("skip.png", media_type="image"))
        manager = FakeGuiManager(entries)
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(asset_manager_gui, "ASSET_MANAGER", manager):
                view = asset_manager_gui.build_browser_view(
                    INPUT_VOLUME, "", "keep", "name_asc", 99, workspace
                )
        self.assertEqual(view.page, 2)
        self.assertEqual(view.page_count, 2)
        self.assertEqual(len(view.gallery), 6)


class GuiOperationTests(unittest.TestCase):
    def test_delete_requires_candidate_and_recurses_for_directory(self) -> None:
        record = asset_manager_gui._record(
            asset("folder", kind="directory", media_type="directory")
        )
        candidate, message, _ = asset_manager_gui.request_delete(record)
        self.assertIn("取り消せません", message)

        manager = FakeGuiManager([])
        with patch.object(asset_manager_gui, "ASSET_MANAGER", manager):
            result = asset_manager_gui.confirm_delete(candidate)
        self.assertIn("削除完了", result)
        self.assertEqual(manager.deleted, [(INPUT_VOLUME, "folder", True)])

    def test_navigation_never_goes_above_root(self) -> None:
        self.assertEqual(asset_manager_gui.navigate_parent(""), ("", 1, ""))
        self.assertEqual(asset_manager_gui.navigate_parent("one"), ("", 1, ""))
        self.assertEqual(
            asset_manager_gui.navigate_parent("one/two"), ("one", 1, "one")
        )

    def test_hugging_face_input_parser_regression(self) -> None:
        repo, filename, revision = _parse_repo_and_filename(
            "https://huggingface.co/Comfy-Org/example/resolve/main/loras/model.safetensors"
        )
        self.assertEqual(repo, "Comfy-Org/example")
        self.assertEqual(filename, "loras/model.safetensors")
        self.assertEqual(revision, "main")


if __name__ == "__main__":
    unittest.main()
