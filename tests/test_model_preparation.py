"""Model placement accepts provider-neutral data; no model downloads in tests."""
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

import preserve_model

ROOT = Path(__file__).resolve().parents[1]


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch.object(preserve_model.app, 'local_entrypoint', return_value=lambda fn: fn):
            self.script = runpy.run_path(str(ROOT / 'scripts/prepare_models.py'))
        self.asset = {'repo_id': 'example/models', 'revision': 'a' * 40,
                      'filename': 'weights/test.bin', 'destination_subdir': 'diffusion_models',
                      'expected_sha256': hashlib.sha256(b'test model').hexdigest()}

    def manifest(self, assets):
        path = self.root / 'models.json'
        path.write_text(json.dumps({'version': 1, 'assets': assets}))
        return str(path)

    def test_forwards_pinned_asset_without_provider_dependency(self):
        with patch.object(preserve_model.preserve_model, 'remote', return_value={'saved': True}) as save:
            self.script['prepare'](self.manifest([self.asset]))
        save.assert_called_once_with(**self.asset)

    def test_invalid_later_asset_rejects_whole_manifest_before_remote_calls(self):
        for changes in ({'revision': 'main'}, {'destination_subdir': '../outside'},
                        {'filename': '/absolute.bin'}, {'destination_subdir': 'unknown'}, {'expected_sha256': 'bad'}, {'extra': 'field'}):
            with self.subTest(changes=changes), patch.object(preserve_model.preserve_model, 'remote') as save:
                with self.assertRaises(ValueError):
                    self.script['prepare'](self.manifest([self.asset, {**self.asset, **changes}]))
                save.assert_not_called()

    def test_checksum_verification_accepts_exact_bytes_and_rejects_mismatch(self):
        target = self.root / 'model'
        target.write_bytes(b'test model')
        preserve_model.verify_sha256(target, self.asset['expected_sha256'])
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            preserve_model.verify_sha256(target, '0' * 64)
