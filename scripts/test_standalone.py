"""Run all tests that do not need the private Ambient extension."""
import importlib.abc
from pathlib import Path
import sys
import unittest


class ForbidAmbient(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"ambient", "ambient_app", "ambient_comfyui"}:
            raise AssertionError("Standalone test imported optional extension: " + fullname)


sys.meta_path.insert(0, ForbidAmbient())
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "tests")]
modules = [path.stem for path in sorted((root / "tests").glob("test_*.py"))
           if not path.name.startswith("test_ambient") and path.name != "test_split_generation.py"]
suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
