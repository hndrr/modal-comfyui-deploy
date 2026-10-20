import unittest
import os
import json
from pathlib import Path
import runpy
from unittest.mock import patch

from comfy_split.evaluation import Evaluation, ui_handle
from comfy_split.storage import VOLUME_NAMES
from scripts.evaluate_servers import model_prompt, validate_manifest


class EvaluationTests(unittest.TestCase):
    def test_model_benchmark_changes_noise_without_mutating_the_fixture(self):
        root = Path(__file__).resolve().parents[2]
        spec = json.loads((root / "benchmarks/minimax_h3_servers.json").read_text())
        before = json.dumps(spec, sort_keys=True)
        prompts = [model_prompt(spec, index) for index in range(3)]
        self.assertEqual([prompt["8"]["inputs"]["noise_seed"] for prompt in prompts], [42, 43, 44])
        self.assertEqual(len({prompt["15"]["inputs"]["filename_prefix"] for prompt in prompts}), 3)
        self.assertEqual(json.dumps(spec, sort_keys=True), before)

    def test_shared_model_mount_is_read_only_and_never_a_cleanup_target(self):
        import modal
        root = Path(__file__).resolve().parents[2]
        original = modal.Volume.with_mount_options
        calls = []

        def record(volume, **kwargs):
            calls.append((volume.name, kwargs))
            return original(volume, **kwargs)

        with patch.dict(os.environ, {"SPLIT_EVALUATION_ID": "unit-test",
                "SPLIT_EVALUATION_BACKEND": "server", "SPLIT_EVALUATION_SHARED_MODELS": "1"}), \
                patch.object(modal.Volume, "with_mount_options", record):
            probe = runpy.run_path(str(root / "scripts/probe_servers.py"))
        self.assertEqual(calls, [(VOLUME_NAMES["models"], {"read_only": True})])
        self.assertEqual(probe["volumes"]["models"].name, VOLUME_NAMES["models"])
        self.assertNotIn(VOLUME_NAMES["models"], Evaluation("unit-test", "server").volumes.values())

    def test_probe_bundles_imported_definition_and_mounts_only_disposable_volumes(self):
        import modal
        from comfy_split.config import Settings

        root = Path(__file__).resolve().parents[2]
        original = modal.Image.add_local_file
        for backend in ("function", "server"):
            sources = []

            def record(image, path, *args, **kwargs):
                sources.append(str(path))
                return original(image, path, *args, **kwargs)

            with self.subTest(backend=backend), patch.dict(os.environ, {
                **Settings().environment(), "SPLIT_EVALUATION_ID": "unit-test",
                "SPLIT_EVALUATION_BACKEND": backend,
                "SPLIT_EVALUATION_SHARED_MODELS": "0",
            }), patch.object(modal.Image, "add_local_file", record):
                probe = runpy.run_path(str(root / "scripts/probe_servers.py"))
            self.assertIn("splitapp.py", sources)
            evaluation = Evaluation("unit-test", backend)
            self.assertEqual(probe["app"].name, evaluation.app_name)
            self.assertEqual({key: volume.name for key, volume in probe["volumes"].items()}, evaluation.volumes)

    def test_every_backend_has_disjoint_storage_and_cannot_name_production(self):
        function = Evaluation("20261020-test", "function")
        server = Evaluation("20261020-test", "server")
        self.assertFalse(set(function.volumes.values()) & set(server.volumes.values()))
        self.assertFalse(set(function.volumes.values()) & set(VOLUME_NAMES.values()))
        self.assertEqual(set(function.volumes), set(VOLUME_NAMES))
        for value in ("", "../production", "test/name", "a" * 21, "UPPER"):
            with self.assertRaises(ValueError):
                Evaluation(value, "server")
        with self.assertRaises(ValueError):
            Evaluation("test", "arbitrary")

    def test_forged_cleanup_manifest_is_rejected_before_any_cloud_call(self):
        evaluation = Evaluation("test", "server")
        valid = dict(run_id="test", backend="server", app_name=evaluation.app_name,
                     volumes=evaluation.volumes)
        validate_manifest(evaluation, valid)
        for replacement in ({"app_name": "comfyui-split"}, {"volumes": VOLUME_NAMES},
                            {"run_id": "other"}, {"backend": "function"}):
            with self.assertRaises(ValueError):
                validate_manifest(evaluation, {**valid, **replacement})

    def test_ui_autoscaler_uses_the_selected_public_sdk_handle(self):
        with patch("modal.Function.from_name") as function, patch("modal.Server.from_name") as server:
            self.assertIs(ui_handle("comfyui-split"), function.return_value)
            function.assert_called_once_with("comfyui-split", "ui")
            self.assertIs(ui_handle("comfyui-servers-eval-test-server", "server"), server.return_value)
            server.assert_called_once_with("comfyui-servers-eval-test-server", "ui")
            with self.assertRaises(ValueError):
                ui_handle("comfyui-split", "invalid")
