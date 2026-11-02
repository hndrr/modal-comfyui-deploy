"""Validate the candidate interpreter, including pins bypassed by install scripts."""

import importlib.metadata
import argparse
import subprocess
import sys
from pathlib import Path

from comfy_split.build_constraints import core_pins


def check_pins(constraints, version=importlib.metadata.version):
    for line in constraints.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, expected = line.strip().split("==", 1)
        actual = version(name)
        if actual != expected:
            raise RuntimeError(
                f"固定依存の競合: {name} は {expected} が必要ですが、{actual} がインストールされています。"
            )


KITCHEN_APIS = ("sol_attn", "sol_attn_chunked", "sol_attn_is_available", "int8_linear")


def check_core_requirements(requirements, version=importlib.metadata.version):
    for pin in core_pins(requirements):
        check_pins(pin, version)


def kitchen_report(constraints, version=importlib.metadata.version, kitchen=None):
    """Read the active interpreter; this never probes CUDA or calls a kernel."""
    expected = next(
        (
            line.split("==", 1)[1].strip()
            for line in constraints.splitlines()
            if line.startswith("comfy-kitchen==")
        ),
        None,
    )
    # Report an unhealthy active venv without blocking the CPU maintenance UI.
    try:
        actual = version("comfy-kitchen")
    except importlib.metadata.PackageNotFoundError:
        actual = None
    if kitchen is None and actual is not None:
        try:
            import comfy_kitchen as kitchen
        except (ImportError, OSError):
            kitchen = None
    return {
        "comfy-kitchen": {
            "version": actual,
            "expected": expected,
            "missingApis": [
                name
                for name in KITCHEN_APIS
                if not callable(getattr(kitchen, name, None))
            ],
        }
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path)
    args = parser.parse_args()
    constraints = Path("/opt/split-constraints.txt").read_text()
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    check_pins(constraints)
    if args.requirements:
        check_core_requirements(args.requirements.read_text())
    report = kitchen_report(constraints)["comfy-kitchen"]
    if not report["expected"] or report["missingApis"]:
        raise RuntimeError(f"Missing comfy-kitchen pin or APIs: {report}")
