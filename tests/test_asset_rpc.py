from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_CACHE_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("COMFY_ASSET_CACHE_DIR", _CACHE_DIR.name)

import asset_rpc  # noqa: E402
from asset_manager import AssetEntry  # noqa: E402


class MaterializeConcurrencyTests(unittest.TestCase):
    def test_parallel_materialize_uses_unique_temporary_files(self) -> None:
        entry = AssetEntry(
            volume="comfy-inputs",
            path="images/example.png",
            name="example.png",
            kind="file",
            size=7,
            modified_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            media_type="image",
        )
        barrier = threading.Barrier(2)
        temp_paths: list[Path] = []
        paths_lock = threading.Lock()
        manager = mock.Mock()

        def download(_entry: AssetEntry, destination: Path) -> None:
            with paths_lock:
                temp_paths.append(Path(destination))
            barrier.wait(timeout=1)
            Path(destination).write_bytes(b"content")

        manager.download_listed_asset.side_effect = download
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "cached.png"
            with (
                mock.patch.object(asset_rpc, "MANAGER", manager),
                mock.patch.object(asset_rpc, "_cached_path", return_value=cached),
                mock.patch.object(asset_rpc, "_evict_cache_if_needed"),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                results = list(executor.map(asset_rpc._materialize, [entry, entry]))

            self.assertEqual(len(set(temp_paths)), 2)
            self.assertTrue(all(".tmp." in path.name for path in temp_paths))
            self.assertTrue(all(not path.exists() for path in temp_paths))
            self.assertEqual(cached.read_bytes(), b"content")
            self.assertTrue(all(result["path"] == cached.as_posix() for result in results))

    def test_stream_lease_survives_cache_file_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cached.mp4"
            workspace = root / "workspace"
            workspace.mkdir()
            source.write_bytes(b"video-bytes")

            with mock.patch.object(asset_rpc, "WORKSPACE", workspace):
                leased = asset_rpc._lease_materialized(
                    {
                        "path": source.as_posix(),
                        "name": "example.mp4",
                        "media_type": "video/mp4",
                        "size": source.stat().st_size,
                    }
                )

            lease_path = Path(leased["path"])
            self.assertNotEqual(lease_path, source)
            self.assertTrue(leased["cleanup"])
            source.unlink()
            self.assertEqual(lease_path.read_bytes(), b"video-bytes")
            lease_path.unlink()

    def test_oversized_asset_is_leased_before_cache_eviction(self) -> None:
        entry = AssetEntry(
            volume="comfy-inputs",
            path="models/oversized.bin",
            name="oversized.bin",
            kind="file",
            size=10,
            modified_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
            media_type="file",
        )
        manager = mock.Mock()
        manager.download_listed_asset.side_effect = lambda _entry, destination: Path(
            destination
        ).write_bytes(b"0123456789")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            thumbs = root / "thumbs"
            workspace = root / "workspace"
            cache.mkdir()
            thumbs.mkdir()
            workspace.mkdir()
            with (
                mock.patch.object(asset_rpc, "MANAGER", manager),
                mock.patch.object(asset_rpc, "CACHE_DIR", cache),
                mock.patch.object(asset_rpc, "THUMB_DIR", thumbs),
                mock.patch.object(asset_rpc, "WORKSPACE", workspace),
                mock.patch.object(asset_rpc, "CACHE_MAX_BYTES", 4),
                mock.patch.object(asset_rpc, "CACHE_MAX_FILES", 64),
                mock.patch.object(asset_rpc, "LEASE_MAX_BYTES", 4),
            ):
                leased = asset_rpc._materialize_for_stream(entry, thumbnail=False)
                cached = asset_rpc._cached_path(entry)

            lease_path = Path(leased["path"])
            self.assertEqual(lease_path.read_bytes(), b"0123456789")
            self.assertFalse(cached.exists())
            lease_path.unlink()

    def test_active_stream_leases_enforce_count_and_byte_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            first_source = root / "first.bin"
            second_source = root / "second.bin"
            first_source.write_bytes(b"123456")
            second_source.write_bytes(b"abcdef")
            payload = {
                "name": "asset.bin",
                "media_type": "application/octet-stream",
                "size": 6,
            }

            with (
                mock.patch.object(asset_rpc, "WORKSPACE", workspace),
                mock.patch.object(asset_rpc, "LEASE_MAX_FILES", 1),
                mock.patch.object(asset_rpc, "LEASE_MAX_BYTES", 10),
            ):
                first = asset_rpc._lease_materialized(
                    {**payload, "path": first_source.as_posix()}
                )
                with self.assertRaisesRegex(RuntimeError, "file limit"):
                    asset_rpc._lease_materialized(
                        {**payload, "path": second_source.as_posix()}
                    )
                Path(first["path"]).unlink()

                with mock.patch.object(asset_rpc, "LEASE_MAX_FILES", 2):
                    first = asset_rpc._lease_materialized(
                        {**payload, "path": first_source.as_posix()}
                    )
                    with self.assertRaisesRegex(RuntimeError, "byte limit"):
                        asset_rpc._lease_materialized(
                            {**payload, "path": second_source.as_posix()}
                        )
                    Path(first["path"]).unlink()


class VideoPosterExtractionTests(unittest.TestCase):
    def test_extract_tries_multiple_strategies_until_one_succeeds(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(cmd))
            out = Path(cmd[-1])
            # Fail first attempt, succeed on second.
            if len(calls) < 2:
                return mock.Mock(returncode=1, stdout=b"", stderr=b"fail")
            out.write_bytes(b"x" * 200)
            return mock.Mock(returncode=0, stdout=b"", stderr=b"")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            dest = root / "poster.jpg"
            source.write_bytes(b"fake-video")
            with (
                mock.patch.object(asset_rpc.shutil, "which", return_value="/usr/bin/ffmpeg"),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                ok = asset_rpc._extract_video_poster(source, dest)

            self.assertTrue(ok)
            self.assertTrue(dest.exists())
            self.assertGreater(dest.stat().st_size, 128)
            self.assertGreaterEqual(len(calls), 2)
            # First strategy seeks to 1s (skip black intro frames).
            self.assertIn("-ss", calls[0])
            self.assertIn("1", calls[0])

    def test_extract_returns_false_when_ffmpeg_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            dest = Path(directory) / "poster.jpg"
            source.write_bytes(b"fake")
            with mock.patch.object(asset_rpc.shutil, "which", return_value=None):
                self.assertFalse(asset_rpc._extract_video_poster(source, dest))
            self.assertFalse(dest.exists())

    def test_thumb_digest_includes_generation_version(self) -> None:
        entry = AssetEntry(
            volume="comfy-outputs",
            path="out/a.mp4",
            name="a.mp4",
            kind="file",
            size=99,
            modified_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
            media_type="video",
        )
        with mock.patch.object(asset_rpc, "THUMB_GEN_VERSION", "v-test"):
            digest_a = asset_rpc._entry_digest(entry, kind="thumb")
        with mock.patch.object(asset_rpc, "THUMB_GEN_VERSION", "v-other"):
            digest_b = asset_rpc._entry_digest(entry, kind="thumb")
        self.assertNotEqual(digest_a, digest_b)
        # File cache keys stay stable across thumb gen bumps.
        file_a = asset_rpc._entry_digest(entry, kind="file")
        with mock.patch.object(asset_rpc, "THUMB_GEN_VERSION", "v-other"):
            file_b = asset_rpc._entry_digest(entry, kind="file")
        self.assertEqual(file_a, file_b)


class MaterializeEntryValidationTests(unittest.TestCase):
    def test_current_server_entry_replaces_supplied_metadata(self) -> None:
        current = AssetEntry(
            volume="comfy-inputs",
            path="asset.png",
            name="asset.png",
            kind="file",
            size=42,
            modified_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
            media_type="image",
        )
        manager = mock.Mock()
        manager._find_entry.return_value = current

        with mock.patch.object(asset_rpc, "MANAGER", manager):
            result = asset_rpc._entry_from_params(
                {
                    "volume": "comfy-inputs",
                    "path": "asset.png",
                    "name": "stale.html",
                    "modified_at": "2025-01-01T00:00:00+00:00",
                    "size": 1,
                    "kind": "file",
                }
            )

        self.assertIs(result, current)
        manager._find_entry.assert_called_once_with("comfy-inputs", "asset.png")

    def test_deleted_asset_never_reaches_cached_materialization(self) -> None:
        manager = mock.Mock()
        manager._find_entry.side_effect = FileNotFoundError("asset does not exist")

        with (
            mock.patch.object(asset_rpc, "MANAGER", manager),
            mock.patch.object(asset_rpc, "_materialize_for_stream") as materialize,
        ):
            for thumbnail in (False, True):
                with self.subTest(thumbnail=thumbnail):
                    response = asset_rpc.handle(
                        {
                            "id": 1,
                            "method": "materialize",
                            "params": {
                                "volume": "comfy-inputs",
                                "path": "deleted.png",
                                "name": "deleted.png",
                                "modified_at": "2025-01-01T00:00:00+00:00",
                                "kind": "file",
                                "thumbnail": thumbnail,
                            },
                        }
                    )
                    self.assertFalse(response["ok"])

        materialize.assert_not_called()


class QueueShutdownTests(unittest.TestCase):
    def test_waits_for_in_progress_work_after_queue_becomes_empty(self) -> None:
        high_q: queue.Queue[str] = queue.Queue()
        low_q: queue.Queue[str] = queue.Queue()
        high_q.put("work")
        self.assertEqual(high_q.get_nowait(), "work")
        self.assertTrue(high_q.empty())
        self.assertEqual(high_q.unfinished_tasks, 1)

        def finish_work() -> None:
            time.sleep(0.03)
            high_q.task_done()

        worker = threading.Thread(target=finish_work)
        worker.start()
        started = time.monotonic()
        asset_rpc._wait_for_queues(
            high_q,
            low_q,
            timeout=0.5,
            poll_interval=0.005,
        )
        elapsed = time.monotonic() - started
        worker.join(timeout=1)

        self.assertGreaterEqual(elapsed, 0.02)
        self.assertEqual(high_q.unfinished_tasks, 0)


class DeleteRetryTests(unittest.TestCase):
    def test_missing_asset_is_an_idempotent_success(self) -> None:
        manager = mock.Mock()
        manager.delete_asset.side_effect = FileNotFoundError("already deleted")

        with (
            mock.patch.object(asset_rpc, "MANAGER", manager),
            mock.patch("time.sleep") as sleep,
        ):
            result = asset_rpc._delete_one_with_retry(
                "comfy-inputs", "missing.png", False, retries=3
            )

        self.assertEqual(result, ("ok", "missing.png", None))
        manager.delete_asset.assert_called_once()
        sleep.assert_not_called()

    def test_transient_failure_is_retried(self) -> None:
        manager = mock.Mock()
        manager.delete_asset.side_effect = [RuntimeError("temporary"), None]

        with (
            mock.patch.object(asset_rpc, "MANAGER", manager),
            mock.patch("time.sleep") as sleep,
        ):
            result = asset_rpc._delete_one_with_retry(
                "comfy-inputs", "retry.png", False, retries=3
            )

        self.assertEqual(result, ("ok", "retry.png", None))
        self.assertEqual(manager.delete_asset.call_count, 2)
        sleep.assert_called_once_with(0.15)


if __name__ == "__main__":
    unittest.main()
