"""Private extension acquisition belongs to the opt-in image build only."""
import os
from pathlib import Path
import runpy
import tomllib
import unittest
from unittest.mock import patch

from comfy_split.extension_sources import AMBIENT, BRIDGE

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

    def test_bridge_node_pack_and_relay_pin_the_same_distribution(self):
        from comfy_split.extension_sources import node_revision
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        source = config["tool"]["uv"]["sources"]["comfyui-agent-bridge"]
        self.assertEqual(source, {"git": "https://github.com/" + BRIDGE["repository"],
                                  "rev": BRIDGE["revision"]})
        self.assertEqual(node_revision(BRIDGE["repository"]), BRIDGE["revision"])
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        package = next(p for p in lock["package"] if p["name"] == "comfyui-agent-bridge")
        self.assertEqual(package["source"]["git"].split("#")[-1], BRIDGE["revision"])

    def test_bridge_only_acquires_private_package_at_build_time(self):
        import comfyapp
        import modal
        with patch.dict(os.environ, {"SPLIT_EXTENSIONS": "", "SPLIT_NODE_PACKS": "",
                                     "SPLIT_AGENT_BRIDGE": "on", "AGENT_RUNTIME_SECRET_NAME": "bridge-secret",
                                     "GITHUB_SECRET_NAME": "extension-reader"}), \
             patch.object(modal.Image, "pip_install_private_repos", autospec=True,
                          side_effect=lambda image, *a, **kw: image) as install:
            app = runpy.run_path(str(ROOT / "splitapp.py"))
        self.assertEqual(app["github_secrets"], [])
        self.assertEqual([s.name for s in app["provider_secrets"]], ["bridge-secret"])
        self.assertEqual(install.call_count, 1)
        self.assertEqual(install.call_args.args[1:],
                         (f"github.com/{BRIDGE['repository']}@{BRIDGE['revision']}",))

    def test_bridge_smoke_image_imports_locally_and_in_container(self):
        import sys
        import modal
        from comfy_split import extension_sources
        # Exercise only module/image construction, never a Modal function call.
        for local in (True, False):
            with self.subTest(local=local), patch.object(modal, "is_local", return_value=local), \
                 patch.dict(sys.modules, {"extension_sources": extension_sources}):
                smoke = runpy.run_path(str(ROOT / "scripts/check_agent_bridge.py"))
            self.assertEqual(smoke["BRIDGE"], BRIDGE)

    def test_retired_app_and_bundled_distribution_are_absent(self):
        self.assertFalse((ROOT / "ambient_app.py").exists())
        self.assertEqual(list((ROOT / "ambient").glob("*.py")), [])
        self.assertEqual(list((ROOT / "vendor").glob("ambient*.whl")), [])
