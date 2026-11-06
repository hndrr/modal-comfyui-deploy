import importlib.util
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from comfy_split.runtime import ComfyProcess

ROOT = Path(__file__).resolve().parents[2]
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
        with (patch.dict(sys.modules, {'execution': SimpleNamespace(PromptQueue=ChangedQueue)}),
              self.assertRaisesRegex(RuntimeError, 'signature')):
            guard.install_cpu_guard()

    def test_image_validation_rejects_an_incompatible_upstream_job_contract(self):
        class Queue:
            def put(self, item):
                pass

        guard = load('build_cpu_guard', PACK / 'cpu_guard.py')
        job = {"id": "split-build-check"}
        jobs = SimpleNamespace(JobStatus=SimpleNamespace(PENDING="pending"),
                               get_job=Mock(return_value=job), get_all_jobs=Mock())
        cli = SimpleNamespace(args=SimpleNamespace(cpu=False))
        kitchen = SimpleNamespace(**{name: lambda: None for name in
                                    ("sol_attn", "sol_attn_chunked", "sol_attn_is_available", "int8_linear")})
        modules = {"execution": SimpleNamespace(PromptQueue=Queue), "comfy.cli_args": cli,
                   "comfy_execution.jobs": jobs, "comfy_kitchen": kitchen}
        run = runpy.run_path
        for compatible in (True, False):
            jobs.get_all_jobs.return_value = ([job], 1) if compatible else ([], 0)
            with self.subTest(compatible=compatible), patch.dict(sys.modules, modules), \
                 patch.object(sys, "argv", ["check_environment", "--bridge"]), \
                 patch.object(sys, "path", sys.path.copy()), \
                 patch("pathlib.Path.read_text", return_value="comfy-kitchen==1\n"), \
                 patch("importlib.metadata.version", return_value="1"), patch("subprocess.run"), \
                 patch("runpy.run_path", return_value={"install_cpu_guard": guard.install_cpu_guard}):
                if compatible:
                    run(str(ROOT / "comfy_split/check_environment.py"), run_name="__main__")
                    self.assertTrue(cli.args.cpu)
                    self.assertTrue(guard.cpu_guard_enabled())
                else:
                    with self.assertRaisesRegex(RuntimeError, "job serialization"):
                        run(str(ROOT / "comfy_split/check_environment.py"), run_name="__main__")

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


class ModalControlPackageTests(unittest.TestCase):
    def test_standard_comfy_loader_contract_without_modal_or_comfy_imports(self):
        root = ROOT / 'extensions/ComfyUI-Modal-Control'
        spec = importlib.util.spec_from_file_location('arbitrary_custom_node', root / '__init__.py',
                                                    submodule_search_locations=[str(root)])
        pack = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pack)
        self.assertEqual(pack.NODE_CLASS_MAPPINGS, {})
        web = root / pack.WEB_DIRECTORY
        self.assertTrue((web / 'modal-control.js').is_file())
        self.assertTrue((web / 'comfy-adapter.mjs').is_file())
