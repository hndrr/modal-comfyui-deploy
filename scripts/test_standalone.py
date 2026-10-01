"""Run the root test suite with optional extension imports forbidden.

Cross-repository tests live in the extension repositories and run separately.
"""
import argparse
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfy_split.config import Settings
from tests.isolation import ForbidOptionalExtensions

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "split", "standard", "assets", "models"), default="all")
    parser.add_argument("--pattern", default="test_*.py", help="unittest discovery filename pattern")
    args = parser.parse_args()
    sys.meta_path.insert(0, ForbidOptionalExtensions())
    root = Path(__file__).resolve().parents[1]
    start = root / "tests" if args.suite == "all" else root / "tests" / args.suite
    # Start plain regardless of shell settings; tests opt in to their own fixtures.
    with patch.dict(os.environ, Settings().environment()):
        suite = unittest.defaultTestLoader.discover(str(start), pattern=args.pattern, top_level_dir=str(root))
        if not suite.countTestCases():
            parser.error("No tests matched the selected suite/pattern")
        raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
