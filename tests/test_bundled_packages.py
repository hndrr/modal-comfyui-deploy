import hashlib
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import bundled_packages

ROOT = Path(__file__).resolve().parents[1]


class BundledPackageTests(unittest.TestCase):
    def test_wheel_manifest_and_lock_pin_the_same_content(self):
        wheel = bundled_packages.ambient_wheel()
        manifest = json.loads((wheel.parent / "ambient-comfyui.json").read_text())
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        package = next(p for p in lock["package"] if p["name"] == manifest["name"])
        self.assertEqual(package["version"], manifest["version"])
        self.assertEqual(package["wheels"][0]["hash"], "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest())
        with ZipFile(wheel) as archive:
            metadata = archive.read(f'ambient_comfyui-{manifest["version"]}.dist-info/METADATA').decode()
            self.assertIn("Version: " + manifest["version"], metadata)

    def test_modified_distribution_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "vendor"
            vendor.mkdir()
            (vendor / "fixture.whl").write_bytes(b"changed")
            (vendor / "ambient-comfyui.json").write_text(json.dumps({"file": "fixture.whl", "sha256": "0" * 64}))
            with patch.object(bundled_packages, "__file__", str(root / "bundled_packages.py")):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    bundled_packages.ambient_wheel()

    def test_retired_app_sources_are_absent(self):
        self.assertFalse((ROOT / "ambient_app.py").exists())
        self.assertEqual(list((ROOT / "ambient").glob("*.py")), [])
