import contextlib
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import beam_runtime


@contextlib.contextmanager
def _volume_mount(boundary: Path):
    """Simulate a Beam Volume.

    Renaming onto it fails with EXDEV, and it refuses the metadata calls
    shutil.copystat makes, which is what broke the shutil.move fallback.
    """

    real_rename = os.rename
    real_utime = os.utime
    real_chmod = os.chmod

    def _on_volume(path) -> bool:
        try:
            return Path(path).is_relative_to(boundary)
        except TypeError:  # file descriptors are never on the volume
            return False

    def fake_rename(src, dst, *args, **kwargs):
        if _on_volume(dst):
            raise OSError(
                errno.EXDEV, "Invalid cross-device link", str(src), None, str(dst)
            )
        return real_rename(src, dst, *args, **kwargs)

    def fake_utime(path, *args, **kwargs):
        if _on_volume(path):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_utime(path, *args, **kwargs)

    def fake_chmod(path, *args, **kwargs):
        if _on_volume(path):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_chmod(path, *args, **kwargs)

    with (
        patch.object(beam_runtime.os, "rename", fake_rename),
        patch.object(beam_runtime.os, "utime", fake_utime),
        patch.object(beam_runtime.os, "chmod", fake_chmod),
    ):
        yield


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
            version=SimpleNamespace(cuda="13.0"),
            __version__="2.10.0+cu130",
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
            result = beam_runtime.validate_acceleration(
                "12.0", required_capabilities="int8_linear,scaled_mm_nvfp4"
            )

        self.assertEqual(result["compute_capability"], "12.0")
        self.assertEqual(result["torch_cuda"], "13.0")
        self.assertEqual(result["driver_major"], "580")
        self.assertIn("scaled_mm_nvfp4", result["kitchen_cuda_capabilities"])

    def test_rejects_wrong_compute_capability(self) -> None:
        kitchen = SimpleNamespace(list_backends=dict)
        with (
            patch.dict(
                sys.modules,
                {
                    "torch": self._torch(capability=(8, 9)),
                    "comfy_kitchen": kitchen,
                },
            ),
            patch("beam_runtime.package_version", return_value="0.2.30"),
            patch("beam_runtime._query_nvidia_smi", return_value=("GPU, 580.1", 580)),
            self.assertRaisesRegex(RuntimeError, "expected 12.0, got 8.9"),
        ):
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
        with (
            patch.dict(
                sys.modules,
                {"torch": self._torch(), "comfy_kitchen": kitchen},
            ),
            patch("beam_runtime.package_version", return_value="0.2.30"),
            patch("beam_runtime._query_nvidia_smi", return_value=("GPU, 580.1", 580)),
            self.assertRaisesRegex(RuntimeError, "extension not found"),
        ):
            beam_runtime.validate_acceleration("12.0")

    def test_rejects_wrong_kitchen_version(self) -> None:
        kitchen = SimpleNamespace(list_backends=dict)
        with (
            patch.dict(
                sys.modules,
                {"torch": self._torch(), "comfy_kitchen": kitchen},
            ),
            patch("beam_runtime.package_version", return_value="0.2.31"),
            patch("beam_runtime._query_nvidia_smi", return_value=("GPU, 580.1", 580)),
            self.assertRaisesRegex(RuntimeError, "expected 0.2.30, got 0.2.31"),
        ):
            beam_runtime.validate_acceleration("12.0")

    def test_rejects_cuda_12_pytorch(self) -> None:
        kitchen = SimpleNamespace(list_backends=dict)
        torch = self._torch()
        torch.version.cuda = "12.8"
        with (
            patch.dict(sys.modules, {"torch": torch, "comfy_kitchen": kitchen}),
            patch("beam_runtime.package_version", return_value="0.2.30"),
            patch("beam_runtime._query_nvidia_smi", return_value=("GPU, 580.1", 580)),
            self.assertRaisesRegex(RuntimeError, "CUDA 13.x is required"),
        ):
            beam_runtime.validate_acceleration("12.0")

    def test_rejects_driver_older_than_580(self) -> None:
        completed = Mock(stdout="NVIDIA A100-SXM4-80GB, 575.57.08\n")
        with (
            patch("beam_runtime.subprocess.run", return_value=completed),
            self.assertRaisesRegex(RuntimeError, "driver 580 or newer"),
        ):
            beam_runtime._query_nvidia_smi()

    def test_rejects_missing_required_kitchen_capability(self) -> None:
        kitchen = SimpleNamespace(
            list_backends=lambda: {
                "cuda": {
                    "available": True,
                    "disabled": False,
                    "unavailable_reason": None,
                    "capabilities": ["int8_linear"],
                }
            }
        )
        with (
            patch.dict(
                sys.modules,
                {"torch": self._torch(), "comfy_kitchen": kitchen},
            ),
            patch("beam_runtime.package_version", return_value="0.2.30"),
            patch("beam_runtime._query_nvidia_smi", return_value=("GPU, 580.1", 580)),
            self.assertRaisesRegex(RuntimeError, "scaled_mm_nvfp4"),
        ):
            beam_runtime.validate_acceleration(
                "12.0", required_capabilities="scaled_mm_nvfp4"
            )

    def test_checks_backend_after_comfyui_quantization_policy(self) -> None:
        state = {"disabled": False}
        kitchen = SimpleNamespace(
            list_backends=lambda: {
                "cuda": {
                    "available": True,
                    "disabled": state["disabled"],
                    "unavailable_reason": None,
                    "capabilities": ["int8_linear"],
                }
            }
        )

        def apply_comfyui_policy(_module_name: str):
            state["disabled"] = True
            return SimpleNamespace()

        with (
            patch.dict(
                sys.modules,
                {"torch": self._torch(), "comfy_kitchen": kitchen},
            ),
            patch("beam_runtime.package_version", return_value="0.2.30"),
            patch("beam_runtime._query_nvidia_smi", return_value=("GPU, 580.1", 580)),
            patch(
                "beam_runtime.importlib.import_module",
                side_effect=apply_comfyui_policy,
            ) as import_module,
            self.assertRaisesRegex(RuntimeError, "CUDA backend is unavailable"),
        ):
            beam_runtime.validate_acceleration(
                "12.0", comfy_roots=(Path("/opt/ComfyUI"),)
            )

        import_module.assert_called_once_with("comfy.quant_ops")


class PersistentStorageTests(unittest.TestCase):
    def test_merge_preserves_persistent_files_and_rehomes_image_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            persistent = root / "persistent"
            image = root / "image"
            (persistent / "node").mkdir(parents=True)
            (image / "node").mkdir(parents=True)

            (persistent / "node" / "settings.json").write_text(
                "user settings", encoding="utf-8"
            )
            (persistent / "node" / "settings.json.conflict").write_text(
                "older image settings", encoding="utf-8"
            )
            (image / "node" / "settings.json").write_text(
                "new image settings", encoding="utf-8"
            )
            (persistent / "node" / "same.txt").write_text("same", encoding="utf-8")
            (image / "node" / "same.txt").write_text("same", encoding="utf-8")
            (image / "node" / "new.txt").write_text("new", encoding="utf-8")

            beam_runtime._merge_directory_contents(persistent, image)

            self.assertEqual(
                (persistent / "node" / "settings.json").read_text(encoding="utf-8"),
                "user settings",
            )
            self.assertEqual(
                (persistent / "node" / "settings.json.conflict").read_text(
                    encoding="utf-8"
                ),
                "older image settings",
            )
            self.assertEqual(
                (persistent / "node" / "settings.json.conflict.1").read_text(
                    encoding="utf-8"
                ),
                "new image settings",
            )
            self.assertEqual(
                (persistent / "node" / "new.txt").read_text(encoding="utf-8"),
                "new",
            )
            self.assertEqual(list(image.iterdir()), [])

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
            self.assertIn("/userdata/{file:.*}", patched)
            self.assertIn(beam_runtime.WORKFLOWS_PATCH_MARKER, patched)

    def test_moves_image_content_onto_a_separate_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            comfy_root = root / "ComfyUI"
            volumes = root / "volumes"
            nodes = comfy_root / "custom_nodes" / "rgthree-comfy"
            nodes.mkdir(parents=True)
            (nodes / "__init__.py").write_text("node", encoding="utf-8")
            (comfy_root / "custom_nodes" / "example.py").write_text(
                "example", encoding="utf-8"
            )
            (comfy_root / "custom_nodes" / "link.py").symlink_to("example.py")
            (comfy_root / "models" / "gligen").mkdir(parents=True)

            with _volume_mount(volumes):
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
                (volumes / "custom_nodes" / "rgthree-comfy" / "__init__.py").read_text(
                    encoding="utf-8"
                ),
                "node",
            )
            self.assertEqual(
                (volumes / "custom_nodes" / "example.py").read_text(encoding="utf-8"),
                "example",
            )
            moved_link = volumes / "custom_nodes" / "link.py"
            self.assertTrue(moved_link.is_symlink())
            self.assertEqual(os.readlink(moved_link), "example.py")
            self.assertTrue((volumes / "models" / "gligen").is_dir())

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
