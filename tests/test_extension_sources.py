"""Private extension acquisition belongs to the opt-in image build only."""
import os
from pathlib import Path
import runpy
import tomllib
import unittest
from unittest.mock import patch

from comfy_split.extension_sources import AMBIENT

ROOT = Path(__file__).resolve().parents[1]


class ExtensionSourceTests(unittest.TestCase):
    def test_deployment_and_local_dependency_pin_the_same_commit(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        source = config["tool"]["uv"]["sources"]["ambient-comfyui"]
        self.assertEqual(source, {"git": "https://github.com/" + AMBIENT["repository"],
                                  "rev": AMBIENT["revision"]})
        self.assertRegex(AMBIENT["revision"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(AMBIENT["revision"], "0" * 40)
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        package = next(p for p in lock["package"] if p["name"] == "ambient-comfyui")
        self.assertEqual(package["version"], AMBIENT["version"])
        self.assertEqual(package["source"]["git"].split("#")[-1], AMBIENT["revision"])

    def test_only_image_build_receives_private_repo_access_when_only_extension_is_enabled(self):
        import comfyapp
        import modal

        original = modal.Image.pip_install_private_repos
        calls = []
        def capture(image, *repositories, **kwargs):
            calls.append((repositories, kwargs))
            return original(image, *repositories, **kwargs)

        with patch.dict(os.environ, {"SPLIT_EXTENSIONS": "ambient", "SPLIT_NODE_PACKS": "",
                                     "SPLIT_AGENT_BRIDGE": "off", "GITHUB_SECRET_NAME": "extension-reader"}), \
             patch.object(modal.Image, "pip_install_private_repos", capture):
            app = runpy.run_path(str(ROOT / "splitapp.py"))
        self.assertEqual(app["provider_secrets"], [])
        self.assertEqual(app["github_secrets"], [])
        self.assertEqual(len(calls), 1)
        repositories, options = calls[0]
        self.assertEqual(repositories, (f"github.com/{AMBIENT['repository']}@{AMBIENT['revision']}",))
        self.assertEqual([secret.name for secret in options["secrets"]], ["extension-reader"])
        self.assertEqual(options["extra_options"], "--no-deps")

    def test_retired_app_and_bundled_distribution_are_absent(self):
        self.assertFalse((ROOT / "ambient_app.py").exists())
        self.assertEqual(list((ROOT / "ambient").glob("*.py")), [])
        self.assertEqual(list((ROOT / "vendor").glob("ambient*.whl")), [])
