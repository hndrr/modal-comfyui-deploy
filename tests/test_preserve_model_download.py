import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import preserve_model


class DownloadPersistenceTests(unittest.TestCase):
    def test_success_is_reported_only_after_the_volume_commit(self):
        self.run_download(commit_error=None)

    def test_commit_failure_is_reported_as_failure(self):
        self.run_download(commit_error=RuntimeError("commit failed"))

    def run_download(self, commit_error):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cached-model"
            source.write_bytes(b"complete model")
            destination = root / "models/diffusion_models/model.safetensors"
            committed = False
            phases = []

            def commit():
                nonlocal committed
                self.assertEqual(destination.read_bytes(), source.read_bytes())
                if commit_error:
                    raise commit_error
                committed = True

            def publish(_call_id, payload):
                phases.append(payload["phase"])
                if payload["phase"] == "done":
                    self.assertTrue(committed)

            hub = SimpleNamespace(hf_hub_download=Mock(return_value=str(source)))
            with (
                patch.dict("sys.modules", {"huggingface_hub": hub}),
                patch.object(preserve_model, "MODEL_DIR", root / "models"),
                patch.object(preserve_model, "volume", SimpleNamespace(commit=commit)),
                patch.object(preserve_model, "_prune_progress"),
                patch.object(preserve_model, "_remote_file_size", return_value=14),
                patch.object(preserve_model, "_publish_progress", side_effect=publish),
            ):
                def download():
                    return preserve_model.preserve_model.local(
                        repo_id="test/model", filename="model.safetensors",
                        destination_subdir="diffusion_models",
                        expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    )

                if commit_error:
                    with self.assertRaisesRegex(RuntimeError, "commit failed"):
                        download()
                    self.assertEqual(phases[-1], "error")
                    self.assertNotIn("done", phases)
                else:
                    result = download()
                    self.assertEqual(result["size_bytes"], source.stat().st_size)
                    self.assertEqual(phases[-1], "done")
