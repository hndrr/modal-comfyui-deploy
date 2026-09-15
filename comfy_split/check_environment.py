"""Validate the candidate interpreter, including pins bypassed by install scripts."""

import importlib.metadata
import subprocess
import sys
from pathlib import Path


def check_pins(constraints, version=importlib.metadata.version):
    for line in constraints.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, expected = line.strip().split("==", 1)
        actual = version(name)
        if actual != expected:
            raise RuntimeError(f"固定依存の競合: {name} は {expected} が必要ですが、{actual} がインストールされています。")


if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    check_pins(Path("/opt/split-constraints.txt").read_text())
