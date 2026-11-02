"""Write pip constraints before installing upstream or custom-node requirements."""

import json
import sys
from importlib.metadata import distribution, distributions
from pathlib import Path
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def wheel_roots():
    # The base installs its GPU wheels from explicit URLs and local wheel files.
    # pip records both in PEP 610 metadata; no second list of wheel names to maintain.
    for dist in distributions():
        origin = dist.read_text("direct_url.json")
        if origin and urlsplit(json.loads(origin)["url"]).path.lower().endswith(".whl"):
            yield dist.metadata["Name"]


def wheel_pins(roots, lookup=distribution):
    pending = [(canonicalize_name(root), "") for root in roots]
    if not pending:
        raise RuntimeError("No base wheel origins found; refusing to install split dependencies")
    pins = {}
    visited = set()
    while pending:
        name, extra = pending.pop()
        if (name, extra) in visited:
            continue
        visited.add((name, extra))
        dist = lookup(name)
        pins[name] = dist.version
        for entry in dist.requires or ():
            req = Requirement(entry)
            if req.marker and not req.marker.evaluate({"extra": extra}):
                continue
            dep = canonicalize_name(req.name)
            pending.extend((dep, value) for value in ("", *req.extras))
    return [f"{name}=={pins[name]}" for name in sorted(pins)]


def core_pins(requirements):
    for name in ("comfy-kitchen", "comfyui-frontend-package"):
        pin = next((line.strip() for line in requirements.splitlines()
                    if line.strip().startswith(name + "==")), None)
        if pin is None:
            raise RuntimeError(f"Upstream ComfyUI must pin {name}; review its requirements")
        yield pin


def build_constraints(requirements, manager):
    return [*wheel_pins(wheel_roots()), *core_pins(requirements), f"comfyui-manager=={manager}"]


if __name__ == "__main__":
    print(*build_constraints(Path(sys.argv[1]).read_text(), sys.argv[2]), sep="\n")
