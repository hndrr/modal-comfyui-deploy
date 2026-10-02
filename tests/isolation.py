"""Import guard shared by the test runner and fresh-interpreter image checks."""
import importlib.abc

from comfy_split.extension_sources import EXTENSIONS, INTEGRATIONS

FORBIDDEN = {
    source["module"].split(".")[0]
    for source in (*EXTENSIONS.values(), *INTEGRATIONS.values())
}


class ForbidOptionalExtensions(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in FORBIDDEN:
            raise AssertionError("Standalone test imported optional extension: " + fullname)
