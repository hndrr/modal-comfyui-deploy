import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comfy_split import runtime


class ImageBrowsingRepairTests(unittest.TestCase):
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
