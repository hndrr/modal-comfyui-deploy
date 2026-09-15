import importlib.util
from pathlib import Path
import unittest


class ModalControlPackageTests(unittest.TestCase):
    def test_standard_comfy_loader_contract_without_modal_or_comfy_imports(self):
        root = Path(__file__).resolve().parents[1] / 'extensions/ComfyUI-Modal-Control'
        spec = importlib.util.spec_from_file_location('arbitrary_custom_node', root / '__init__.py',
                                                    submodule_search_locations=[str(root)])
        pack = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pack)
        self.assertEqual(pack.NODE_CLASS_MAPPINGS, {})
        web = root / pack.WEB_DIRECTORY
        self.assertTrue((web / 'modal-control.js').is_file())
        self.assertTrue((web / 'comfy-adapter.mjs').is_file())
