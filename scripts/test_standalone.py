"""Run the root test suite with optional extension imports forbidden.

Cross-repository tests live in tests/integration and are run separately.
"""
import importlib.abc
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfy_split.extension_sources import EXTENSIONS, INTEGRATIONS

FORBIDDEN = {source["module"].split(".")[0] for source in (*EXTENSIONS.values(), *INTEGRATIONS.values())}


class ForbidOptionalExtensions(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in FORBIDDEN:
            raise AssertionError("Standalone test imported optional extension: " + fullname)


if __name__ == "__main__":
    sys.meta_path.insert(0, ForbidOptionalExtensions())
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
