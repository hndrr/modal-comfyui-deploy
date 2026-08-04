from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

_CACHE_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("COMFY_ASSET_CACHE_DIR", _CACHE_DIR.name)

import asset_rpc  # noqa: E402


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
