import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from comfy_split.runtime import ComfyProcess

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'extensions/ComfyUI-Modal-Bridge'


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BridgeTests(unittest.TestCase):
    def test_extension_is_inert_outside_modal(self):
        with patch.dict('os.environ', {'SPLIT_INTEGRATION': '0'}):
            module = load('ordinary_comfy_bridge', PACK / '__init__.py')
        self.assertEqual(module.NODE_CLASS_MAPPINGS, {})

    def test_guard_only_changes_put_and_rejects_unknown_contract(self):
        class Queue:
            def put(self, item):
                pass
            def get_history(self):
                return {'ordinary': True}
        original_history = Queue.get_history
        guard = load('cpu_guard_test', PACK / 'cpu_guard.py')
        with patch.dict(sys.modules, {'execution': SimpleNamespace(PromptQueue=Queue)}):
            guard.install_cpu_guard()
            guard.install_cpu_guard()
            self.assertTrue(guard.cpu_guard_enabled())
            with self.assertRaisesRegex(RuntimeError, 'dispatcher'):
                Queue().put('workflow')
            self.assertIs(Queue.get_history, original_history)
        class ChangedQueue:
            def put(self, changed, another):
                pass
        with patch.dict(sys.modules, {'execution': SimpleNamespace(PromptQueue=ChangedQueue)}):
            with self.assertRaisesRegex(RuntimeError, 'signature'):
                guard.install_cpu_guard()

    def test_temporary_output_survives_normal_cleanup_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = ComfyProcess('gpu', 8188)
            process.temp_root = root / 'local'
            source = process.temp_root / 'temp' / 'nested'
            source.mkdir(parents=True)
            (source / 'preview.png').write_bytes(b'image')
            original = {'images': [{'type': 'temp', 'filename': 'preview.png', 'subfolder': 'nested'}]}
            with patch('comfy_split.runtime.TEMP_ARCHIVE', root / '.split-temp'):
                process.archive_temp()
            result = process.durable_outputs(original)['images'][0]
            self.assertEqual(original['images'][0]['type'], 'temp')
            self.assertEqual(result['type'], 'output')
            import shutil
            shutil.rmtree(process.temp_root)
            self.assertEqual((root / result['subfolder'] / result['filename']).read_bytes(), b'image')
            self.assertNotEqual(process.temp_namespace, ComfyProcess('gpu', 8188).temp_namespace)
            with self.assertRaises(ValueError):
                process.durable_outputs({'type': 'temp', 'filename': 'x', 'subfolder': '../escape'})
