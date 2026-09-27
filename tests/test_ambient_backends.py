import unittest
from pathlib import Path
import tempfile
import hashlib
from ambient_fixtures import DEFAULT_BACKENDS, workflow, validate_object_info, object_info, request
from model_manifests import FAST_MODEL_FILES, MODEL_FILES, comfy_assets

class FastWorkflowTest(unittest.TestCase):
    def test_fast_recipe_uses_requested_models_native_vsa_and_four_euler_steps(self):
        for resolution in ("preview", "quality"):
            graph = workflow(
                request(mode="fasth3", resolution=resolution), object_info=object_info()
            )
            kinds = [node["class_type"] for node in graph.values()]
            self.assertNotIn("LoraLoaderModelOnly", kinds)
            self.assertNotIn("LoadImage", kinds)
            self.assertEqual(
                graph["1"]["inputs"]["unet_name"], FAST_MODEL_FILES["unet"]
            )
            self.assertEqual(
                graph["4"]["inputs"]["vae_name"], FAST_MODEL_FILES["video_vae"]
            )
            self.assertEqual(graph["5"]["inputs"]["vae_name"], MODEL_FILES["audio_vae"])
            vsa = graph["2"]["inputs"]
            self.assertEqual(vsa["selection"], "vsa")
            self.assertEqual(vsa["selection.keep_percent"], 10)
            self.assertEqual((vsa["start_percent"], vsa["end_percent"]), (0, 1))
            self.assertEqual(vsa["sink_conditioning"], "exact_kv_and_rows")
            self.assertEqual(graph["9"]["inputs"]["sampler_name"], "euler")
            sigmas = [
                float(value) for value in graph["10"]["inputs"]["sigmas"].split(",")
            ]
            self.assertEqual(len(sigmas), 5)
            for actual, base in zip(sigmas, (1, 0.75, 0.5, 0.25, 0), strict=True):
                self.assertAlmostEqual(actual, 12 * base / (1 + 11 * base))
            self.assertEqual(graph["13"]["class_type"], "VAEDecodeAudio")
            self.assertTrue(validate_object_info(object_info(), "fasth3"))
        h3 = workflow(request(), object_info=object_info())
        self.assertEqual(h3["4"]["inputs"]["vae_name"], MODEL_FILES["video_vae"])
        self.assertEqual(h3["10"]["inputs"]["steps"], 8)


    def test_dynamic_branch_uses_live_defaults_and_types(self):
        info = object_info()
        option = info["BlockSparseAttention"]["input"]["required"]["selection"][1][
            "options"
        ][1]
        option["inputs"]["required"]["added"] = ["FLOAT", {"default": 0.2}]
        info["MiniMaxH3SigmaShift"]["output"] = ["STRING", "MODEL"]
        graph = workflow(request(mode="fasth3"), object_info=info)
        self.assertEqual(graph["2"]["inputs"]["selection.added"], 0.2)
        self.assertEqual(graph["2"]["inputs"]["model"], ["17", 1])
        self.assertNotIn("selection.tau", graph["2"]["inputs"])


    def test_unavailable_fast_contracts_are_rejected_before_submission(self):
        for change in ("node", "vsa", "child", "required", "model", "vae"):
            info = object_info()
            selection = info["BlockSparseAttention"]["input"]["required"]["selection"][
                1
            ]
            if change == "node":
                del info["BlockSparseAttention"]
            elif change == "vsa":
                selection["options"] = selection["options"][:1]
            elif change == "child":
                del selection["options"][1]["inputs"]["required"]["keep_percent"]
            elif change == "required":
                selection["options"][1]["inputs"]["required"]["unknown"] = ["EMBEDDING"]
            elif change == "model":
                info["UNETLoader"]["input"]["required"]["unet_name"] = [
                    [MODEL_FILES["unet"]]
                ]
            else:
                info["VAELoader"]["input"]["required"]["vae_name"] = [
                    ["minimax_h3_video_vae_fp16.safetensors", MODEL_FILES["audio_vae"]]
                ]
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_object_info(info, "fasth3")
        with self.assertRaises(ValueError):
            workflow(request(mode="fasth3"), "anchor.png", object_info=object_info())


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
