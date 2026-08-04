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
