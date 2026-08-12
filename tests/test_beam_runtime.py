import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import beam_runtime


class BuildLaunchCommandTests(unittest.TestCase):
    def test_adds_sage_attention_by_default(self) -> None:
        command = beam_runtime.build_launch_command("--cpu-vae", sage_attention=True)
        self.assertIn("--use-sage-attention", command)
        self.assertEqual(command[-1], "--cpu-vae")

    def test_does_not_duplicate_explicit_sage_attention(self) -> None:
        command = beam_runtime.build_launch_command(
            "--use-sage-attention --cpu-vae", sage_attention=True
        )
        self.assertEqual(command.count("--use-sage-attention"), 1)

    def test_can_disable_sage_attention(self) -> None:
        command = beam_runtime.build_launch_command("", sage_attention=False)
        self.assertNotIn("--use-sage-attention", command)


class AccelerationValidationTests(unittest.TestCase):
    @staticmethod
    def _torch(*, available: bool = True, capability: tuple[int, int] = (12, 0)):
        cuda = SimpleNamespace(
            is_available=lambda: available,
            current_device=lambda: 0,
            get_device_capability=lambda _device: capability,
            get_device_name=lambda _device: "NVIDIA GeForce RTX 5090",
        )
        return SimpleNamespace(
            cuda=cuda,
            version=SimpleNamespace(cuda="12.8"),
            __version__="2.10.0+cu128",
        )

    def test_validates_cuda_arch_and_kitchen_backend(self) -> None:
        kitchen = SimpleNamespace(
            list_backends=lambda: {
                "cuda": {
                    "available": True,
                    "disabled": False,
                    "unavailable_reason": None,
                    "capabilities": ["int8_linear", "scaled_mm_nvfp4"],
                }
            }
        )
        completed = Mock(stdout="NVIDIA GeForce RTX 5090, 580.126.18\n")
        with (
            patch.dict(
                sys.modules,
                {"torch": self._torch(), "comfy_kitchen": kitchen},
            ),
            patch("beam_runtime.subprocess.run", return_value=completed),
            patch("beam_runtime.package_version", return_value="0.2.30"),
        ):
            result = beam_runtime.validate_acceleration("12.0")

        self.assertEqual(result["compute_capability"], "12.0")
        self.assertEqual(result["torch_cuda"], "12.8")
        self.assertIn("scaled_mm_nvfp4", result["kitchen_cuda_capabilities"])

    def test_rejects_wrong_compute_capability(self) -> None:
        kitchen = SimpleNamespace(list_backends=lambda: {})
        with patch.dict(
            sys.modules,
            {
                "torch": self._torch(capability=(8, 9)),
                "comfy_kitchen": kitchen,
            },
        ), patch("beam_runtime.package_version", return_value="0.2.30"):
            with self.assertRaisesRegex(RuntimeError, "expected 12.0, got 8.9"):
                beam_runtime.validate_acceleration("12.0")

    def test_rejects_missing_kitchen_cuda_backend(self) -> None:
        kitchen = SimpleNamespace(
            list_backends=lambda: {
                "cuda": {
                    "available": False,
                    "disabled": False,
                    "unavailable_reason": "extension not found",
                }
            }
        )
        with patch.dict(
            sys.modules,
            {"torch": self._torch(), "comfy_kitchen": kitchen},
        ), patch("beam_runtime.package_version", return_value="0.2.30"):
            with self.assertRaisesRegex(RuntimeError, "extension not found"):
                beam_runtime.validate_acceleration("12.0")

    def test_rejects_wrong_kitchen_version(self) -> None:
        kitchen = SimpleNamespace(list_backends=lambda: {})
        with (
            patch.dict(
                sys.modules,
                {"torch": self._torch(), "comfy_kitchen": kitchen},
            ),
            patch("beam_runtime.package_version", return_value="0.2.31"),
        ):
            with self.assertRaisesRegex(RuntimeError, "expected 0.2.30, got 0.2.31"):
                beam_runtime.validate_acceleration("12.0")


class PersistentStorageTests(unittest.TestCase):
    def test_moves_image_content_and_links_all_persistent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            comfy_root = root / "ComfyUI"
            volumes = root / "volumes"
            (comfy_root / "models" / "checkpoints").mkdir(parents=True)
            (comfy_root / "models" / "checkpoints" / "bundled.txt").write_text(
                "model", encoding="utf-8"
            )
            user_manager = comfy_root / "app" / "user_manager.py"
            user_manager.parent.mkdir(parents=True)
            user_manager.write_text(
                '@routes.get("/userdata/{file}")\nALLOWED_TYPES = []\n',
                encoding="utf-8",
            )

            beam_runtime.prepare_persistent_storage(
                comfy_roots=(comfy_root,),
                models=volumes / "models",
                custom_nodes=volumes / "custom_nodes",
                output=volumes / "output",
                input_dir=volumes / "input",
                user=volumes / "user",
            )

            for directory in ("models", "custom_nodes", "output", "input", "user"):
                self.assertTrue((comfy_root / directory).is_symlink())
            self.assertEqual(
                (volumes / "models" / "checkpoints" / "bundled.txt").read_text(
                    encoding="utf-8"
                ),
                "model",
            )
            patched = user_manager.read_text(encoding="utf-8")
            self.assertIn('/userdata/{file:.*}', patched)
            self.assertIn(beam_runtime.WORKFLOWS_PATCH_MARKER, patched)

    def test_missing_comfyui_installation_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(FileNotFoundError, "ComfyUI installation"):
                beam_runtime.prepare_persistent_storage(
                    comfy_roots=(root / "missing",),
                    models=root / "models",
                    custom_nodes=root / "custom_nodes",
                    output=root / "output",
                    input_dir=root / "input",
                    user=root / "user",
                )


if __name__ == "__main__":
    unittest.main()
