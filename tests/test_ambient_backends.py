"""Route compatibility, native FastH3 binding and preparation without GPU imports."""

from copy import deepcopy
import hashlib
import importlib.metadata
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ambient.contracts import ROUTES, fingerprint, validate_request
from ambient.h3 import workflow
from ambient.models import FAST_MODEL_FILES, MODEL_FILES, comfy_assets, references
from ambient.readiness import describe_modes, validate_object_info
from ambient.service import Conflict, JobService
from ambient.split import check_dependencies
from comfy_split.check_environment import KITCHEN_APIS, check_pins, kitchen_report
from ambient_fixtures import object_info
from test_ambient import Store, request


class BackendContractTest(unittest.TestCase):
    def test_supported_matrix_and_legacy_defaults(self):
        for mode, backend in ROUTES:
            self.assertEqual(
                validate_request(request(mode=mode, backend=backend))["backend"],
                backend,
            )
            if mode == "fasth3":
                for key in ("imageId", "parentClipId"):
                    with self.assertRaises(ValueError):
                        validate_request(
                            request(
                                mode=mode,
                                backend=backend,
                                **{key: request()["requestId"]},
                            )
                        )
        self.assertEqual(validate_request(request())["backend"], "comfyui")
        self.assertEqual(
            validate_request(request(mode="fasth3"))["backend"], "fastvideo"
        )
        for mode, backend in (
            ("h3", "fastvideo"),
            ("fasth3", "unknown"),
            ("h3", None),
            ("h3", []),
        ):
            with (
                self.subTest(mode=mode, backend=backend),
                self.assertRaises(ValueError),
            ):
                validate_request(request(mode=mode, backend=backend))

    def test_legacy_job_retry_normalizes_backend_without_dispatch_or_rewrite(self):
        for mode, default in (("h3", "comfyui"), ("fasth3", "fastvideo")):
            store, spawn = Store(), Mock(return_value="fc-1")
            service = JobService(store, spawn)
            old = validate_request(request(mode=mode))
            old.pop("backend")
            job_id = old["requestId"]
            store[job_id] = {
                "id": job_id,
                "status": "completed",
                "request": old,
                "fingerprint": fingerprint(old),
            }
            before = deepcopy(store[job_id])
            self.assertEqual(service.submit(old)["backend"], default)
            self.assertEqual(service.submit({**old, "backend": default})["id"], job_id)
            if mode == "fasth3":
                with self.assertRaises(Conflict):
                    service.submit({**old, "backend": "comfyui"})
            self.assertEqual(store[job_id], before)
            spawn.assert_not_called()

    def test_new_job_keeps_backend_and_source_references(self):
        store, spawn = Store(), Mock(return_value="fc-1")
        service = JobService(
            store, spawn, reference=lambda req: references(req["mode"], req["backend"])
        )
        req = request(mode="fasth3", backend="comfyui")
        result = service.submit(req)
        self.assertEqual(result["backend"], "comfyui")
        self.assertEqual(result["references"]["models"], comfy_assets("fasth3"))
        self.assertEqual(service.submit(req), result)
        with self.assertRaises(Conflict):
            service.submit({**req, "backend": "fastvideo"})
        spawn.assert_called_once()


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
                    [MODEL_FILES["video_vae"], MODEL_FILES["audio_vae"]]
                ]
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_object_info(info, "fasth3")
        with self.assertRaises(ValueError):
            workflow(request(mode="fasth3"), "anchor.png", object_info=object_info())


class PreparationTest(unittest.TestCase):
    def test_missing_package_is_reported_without_breaking_cpu_diagnostics(self):
        version = Mock(
            side_effect=importlib.metadata.PackageNotFoundError("comfy-kitchen")
        )
        report = kitchen_report("comfy-kitchen==0.2.33", version)["comfy-kitchen"]
        self.assertIsNone(report["version"])
        self.assertEqual(report["expected"], "0.2.33")
        self.assertEqual(report["missingApis"], list(KITCHEN_APIS))
        with self.assertRaisesRegex(ValueError, "comfy-kitchen"):
            check_dependencies({"dependencies": {"comfy-kitchen": report}}, "fasth3")

    def test_readiness_is_per_route_and_invalidates_changed_references(self):
        url, revision = "https://comfy.example", "a" * 40
        jobs = {
            "prepared:h3": {"url": url, "backend": "split"},
            "prepared:fasth3": {"revision": revision},
        }
        modes = describe_modes(jobs, url, revision)
        self.assertTrue(modes["fasth3"]["ready"])
        self.assertFalse(modes["fasth3"]["backends"]["comfyui"]["ready"])
        record = {
            "url": url,
            "backend": "split",
            "references": references("fasth3", "comfyui"),
            "gpuValidated": False,
        }
        jobs["prepared:fasth3:comfyui"] = record
        modes = describe_modes(jobs, url, "")
        self.assertFalse(modes["fasth3"]["ready"])
        self.assertTrue(modes["fasth3"]["backends"]["comfyui"]["ready"])
        self.assertFalse(
            modes["fasth3"]["backends"]["comfyui"]["validation"]["gpuValidated"]
        )
        record["references"]["models"][0]["revision"] = "obsolete"
        self.assertFalse(
            describe_modes(jobs, url, revision)["fasth3"]["backends"]["comfyui"][
                "ready"
            ]
        )

    def test_missing_or_mismatched_kitchen_is_rejected_without_gpu_probe(self):
        kitchen = SimpleNamespace(**{name: Mock() for name in KITCHEN_APIS})
        report = kitchen_report("comfy-kitchen==0.2.33\n", lambda _: "0.2.33", kitchen)
        check_dependencies({"dependencies": report}, "fasth3")
        for api in KITCHEN_APIS:
            getattr(kitchen, api).assert_not_called()
        for bad in (
            {},
            {"version": "0.2.1", "expected": "0.2.33", "missingApis": []},
            {"version": "0.2.33", "expected": "0.2.33", "missingApis": ["sol_attn"]},
        ):
            with self.assertRaisesRegex(ValueError, "comfy-kitchen"):
                check_dependencies({"dependencies": {"comfy-kitchen": bad}}, "fasth3")
        with self.assertRaises(RuntimeError):
            check_pins("comfy-kitchen==0.2.33", lambda _: "0.2.1")
        del kitchen.sol_attn
        self.assertIn(
            "sol_attn",
            kitchen_report("comfy-kitchen==0.2.33", lambda _: "0.2.33", kitchen)[
                "comfy-kitchen"
            ]["missingApis"],
        )

    def test_manifest_and_checksum_verification(self):
        from preserve_model import verify_sha256

        fast, h3 = comfy_assets("fasth3"), comfy_assets("h3")
        self.assertEqual((len(fast), len(h3)), (4, 5))
        self.assertFalse(any(asset["destination_subdir"] == "loras" for asset in fast))
        self.assertEqual(sum("expected_sha256" in asset for asset in fast), 2)
        self.assertTrue(all(len(asset["revision"]) == 40 for asset in fast + h3))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model"
            target.write_bytes(b"test model")
            verify_sha256(target, hashlib.sha256(b"test model").hexdigest())
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                verify_sha256(target, "0" * 64)
