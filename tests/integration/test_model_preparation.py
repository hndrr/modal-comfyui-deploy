"""Modal-owned model manifest and checksum verification; never downloads models."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from model_manifests import PREPARATION_MODES as DEFAULT_BACKENDS, comfy_assets

class PreparationTest(unittest.TestCase):
    def test_manifest_and_checksum_verification(self):
        from preserve_model import verify_sha256

        fast, h3 = comfy_assets("fasth3"), comfy_assets("h3")
        self.assertEqual((len(fast), len(h3)), (4, 5))
        self.assertFalse(any(asset["destination_subdir"] == "loras" for asset in fast))
        self.assertEqual(sum("expected_sha256" in asset for asset in fast), 2)
        self.assertTrue(all(len(asset["revision"]) == 40 for asset in fast + h3))
        for mode in DEFAULT_BACKENDS:
            video_vae = next(asset for asset in comfy_assets(mode)
                             if asset["filename"].endswith("minimax_h3_video_vae_int8_convrot.safetensors"))
            self.assertEqual(video_vae, {
                "repo_id": "Kijai/MiniMax-H3-experimental",
                "revision": "f4cac997f880e93cf6940af61ee8d58ef31ff7f3",
                "filename": "minimax_h3_video_vae_int8_convrot.safetensors",
                "destination_subdir": "vae",
                "expected_sha256": "9bb2d96f218c76babd85e0611b85ca8fb330a90546c01a0005e8a58a59593410",
            })
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model"
            target.write_bytes(b"test model")
            verify_sha256(target, hashlib.sha256(b"test model").hexdigest())
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                verify_sha256(target, "0" * 64)
