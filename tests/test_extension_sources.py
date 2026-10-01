"""Private extension acquisition belongs to the opt-in image build only."""
import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from comfy_split.config import Settings
from comfy_split.extension_sources import EXTENSIONS, INTEGRATIONS, NODE_SOURCES, node_revision

ROOT = Path(__file__).resolve().parents[1]


class ExtensionSourceTests(unittest.TestCase):
    def test_registered_sources_have_immutable_pins(self):
        for name, source in (*EXTENSIONS.items(), *INTEGRATIONS.items()):
            with self.subTest(extension=name):
                self.assertRegex(source["revision"], r"^[0-9a-f]{40}$")
                self.assertNotEqual(source["revision"], "0" * 40)
                self.assertTrue(source["distribution"])
                self.assertTrue(source["module"])
                self.assertTrue(source["factory"])

    def test_selected_packages_share_build_path_and_limit_runtime_credentials(self):
        import comfyapp
        import modal
        for name, source in (*EXTENSIONS.items(), *INTEGRATIONS.items()):
            environment = {**Settings().environment(), "GITHUB_SECRET_NAME": "extension-reader"}
            if "setting" in source:
                environment[source["setting"]] = "on"
            else:
                environment["SPLIT_EXTENSIONS"] = name
            secrets = {key: "runtime-secret-" + str(i) for i, (key, _) in enumerate(source.get("secrets", []))}
            with self.subTest(extension=name), patch.dict(os.environ, {**environment, **secrets}), \
                 patch.object(modal.Image, "pip_install_private_repos", autospec=True,
                              side_effect=lambda image, *a, **kw: image) as install:
                app = runpy.run_path(str(ROOT / "splitapp.py"))
            self.assertEqual(app["github_secrets"], [])
            self.assertEqual([s.name for s in app["provider_secrets"]], list(secrets.values()))
            self.assertEqual(install.call_count, 1)
            self.assertEqual(install.call_args.args[1:],
                             (f"github.com/{source['repository']}@{source['revision']}",))
            self.assertEqual([secret.name for secret in install.call_args.kwargs["secrets"]], ["extension-reader"])
            self.assertEqual(install.call_args.kwargs["extra_options"], "--no-deps")

    def test_referenced_nodes_share_their_adapter_pin(self):
        for source in INTEGRATIONS.values():
            if source in NODE_SOURCES.values():
                self.assertEqual(node_revision(source["repository"]), source["revision"])

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
