import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from comfy_split import runtime
from comfy_split.check_environment import check_pins


class EnvironmentTests(unittest.TestCase):
    def test_candidate_rejects_install_scripts_that_override_protected_packages(self):
        check_pins("torch==2.10.0+cu130\n", lambda _: "2.10.0+cu130")
        with self.assertRaisesRegex(RuntimeError, "固定依存の競合"):
            check_pins("torch==2.10.0+cu130\n", lambda _: "2.11.0")

    def test_new_base_copies_nodes_without_copying_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template"
            (template / "custom_nodes/pack").mkdir(parents=True)
            (template / "custom_nodes/pack/__init__.py").write_text("node")
            (template / "main.py").write_text("core")
            with patch.object(runtime, "TEMPLATE", template), patch.object(runtime, "ENVIRONMENTS", root / "envs"), \
                 patch.object(runtime, "USER", root / "user"), patch.object(runtime.subprocess, "run", Mock()):
                runtime.initialize_environment()
            self.assertFalse((root / "envs/base/comfy/main.py").exists())
            self.assertTrue((root / "envs/base/comfy/custom_nodes/pack/__init__.py").exists())

    def test_repair_clones_active_environment_and_preserves_other_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environments = root / "environments"
            template = root / "template"
            source = environments / "env-old"
            node = "comfy/custom_nodes/ComfyUI-Image-Browsing"
            (source / node).mkdir(parents=True)
            (source / node / "pyproject.toml").write_text('version = "2.3.1"')
            other = source / "comfy/custom_nodes/comfyui-gguf"
            other.mkdir()
            (other / "__init__.py").write_text("# installed node")
            (source / "venv/bin").mkdir(parents=True)
            (source / "venv/bin/pip").write_text(f"#!{source}/venv/bin/python\n")
            bundled = template / "custom_nodes/ComfyUI-Image-Browsing"
            (bundled / "web").mkdir(parents=True)
            (bundled / "pyproject.toml").write_text('version = "2.3.0"')
            (bundled / "web/version.yaml").write_text("version: 2.3.0")
            (bundled / "web/manager.js").write_text("// bundled frontend")
            with patch.object(runtime, "ENVIRONMENTS", environments), patch.object(runtime, "TEMPLATE", template):
                version = runtime.create_environment("env-old", restore_image_browsing=True)
                unchanged = runtime.create_environment("env-old")
            repaired = environments / version
            self.assertIn("2.3.0", (repaired / node / "pyproject.toml").read_text())
            self.assertTrue((repaired / node / "web/manager.js").is_file())
            self.assertEqual((repaired / "comfy/custom_nodes/comfyui-gguf/__init__.py").read_text(), "# installed node")
            self.assertIn(str(repaired), (repaired / "venv/bin/pip").read_text())
            self.assertIn("2.3.1", (source / node / "pyproject.toml").read_text())
            self.assertIn("2.3.1", (environments / unchanged / node / "pyproject.toml").read_text())
