"""Validate the candidate interpreter, including pins bypassed by install scripts."""

import importlib.metadata
import argparse
import subprocess
import runpy
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
    """Reject core package versions that differ from the selected ComfyUI requirements."""
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
    parser.add_argument("--bridge", action="store_true", help="Check upstream split contracts on the build CPU")
    args = parser.parse_args()
    constraints = Path("/opt/split-constraints.txt").read_text()
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    check_pins(constraints)
    if args.requirements:
        check_core_requirements(args.requirements.read_text())
    report = kitchen_report(constraints)["comfy-kitchen"]
    if not report["expected"] or report["missingApis"]:
        raise RuntimeError(f"Missing comfy-kitchen pin or APIs: {report}")
    if args.bridge:
        sys.path.insert(0, "/opt/comfy-template")
        from comfy.cli_args import args as comfy_args
        comfy_args.cpu = True
        bridge = Path("/opt/comfy-extensions/ComfyUI-Modal-Bridge")
        try:
            runpy.run_path(str(bridge / "cpu_guard.py"))["verify_cpu_guard"]()
        except Exception as error:
            raise RuntimeError("Incompatible ComfyUI CPU queue guard; check Bridge cpu_guard.py") from error
        job_id = "split-build-check"
        body = {"queue": {"queue_running": [], "queue_pending": [[0, job_id, {}, {"create_time": 1}, []]]},
                "history": {}, "query": {"status": "pending", "limit": "1"}}
        try:
            serialize = runpy.run_path(str(bridge / "jobs.py"))["serialize_jobs"]
            listing, list_status = serialize(body)
            job, job_status = serialize({**body, "job_id": job_id})
            if (list_status != 200 or job_status != 200 or listing["pagination"]["total"] != 1
                    or [item["id"] for item in listing["jobs"]] != [job_id] or job["id"] != job_id):
                raise ValueError("Expected the queued job in both list and detail responses")
        except Exception as error:
            raise RuntimeError("Incompatible ComfyUI job serialization; check Bridge jobs.py") from error
