"""Private extension acquisition belongs to the opt-in image build only."""
import os
from pathlib import Path
import runpy
import tomllib
import unittest
from unittest.mock import patch

from comfy_split.extension_sources import EXTENSIONS, BRIDGE

ROOT = Path(__file__).resolve().parents[1]


class ExtensionSourceTests(unittest.TestCase):
    def test_registered_sources_have_immutable_pins(self):
        for name, source in EXTENSIONS.items():
            with self.subTest(extension=name):
                self.assertRegex(source["revision"], r"^[0-9a-f]{40}$")
                self.assertNotEqual(source["revision"], "0" * 40)
                self.assertTrue(source["distribution"])
                self.assertTrue(source["module"])
                self.assertTrue(source["factory"])

    def test_only_image_build_receives_private_repo_access_when_only_extension_is_enabled(self):
        import comfyapp
        import modal

        original = modal.Image.pip_install_private_repos
        calls = []
        def capture(image, *repositories, **kwargs):
            calls.append((repositories, kwargs))
            return original(image, *repositories, **kwargs)

        name, source = next(iter(EXTENSIONS.items()))
        with patch.dict(os.environ, {"SPLIT_EXTENSIONS": name, "SPLIT_NODE_PACKS": "",
                                     "SPLIT_AGENT_BRIDGE": "off", "GITHUB_SECRET_NAME": "extension-reader"}), \
             patch.object(modal.Image, "pip_install_private_repos", capture):
            app = runpy.run_path(str(ROOT / "splitapp.py"))
        self.assertEqual(app["provider_secrets"], [])
        self.assertEqual(app["github_secrets"], [])
        self.assertEqual(len(calls), 1)
        repositories, options = calls[0]
        self.assertEqual(repositories, (f"github.com/{source['repository']}@{source['revision']}",))
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

    def test_frontend_install_copies_assets_without_registering_native_nodes(self):
        import tempfile
        from comfy_split import extension_frontend
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'package'
            (package / 'assets').mkdir(parents=True)
            (package / 'assets/panel.js').write_text('export const panel = true;')
            target = root / 'extensions'
            target.mkdir()
            catalog = {'sample': {'web_package': 'test_plugin', 'web_directory': 'assets', 'web_name': 'Sample'}}
            with patch.dict(extension_frontend.EXTENSIONS, catalog, clear=True), \
                 patch.object(extension_frontend, 'files', return_value=package) as resources:
                extension_frontend.install('sample', target)
            resources.assert_called_once_with('test_plugin')
            registered = runpy.run_path(str(target / 'Sample/__init__.py'))
            self.assertEqual(registered['NODE_CLASS_MAPPINGS'], {})
            self.assertEqual(registered['WEB_DIRECTORY'], './web')
            self.assertEqual((target / 'Sample/web/panel.js').read_bytes(),
                             (package / 'assets/panel.js').read_bytes())
