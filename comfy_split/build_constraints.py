"""Write pip constraints before installing upstream or custom-node requirements."""

import json
import re
import sys
from importlib.metadata import distribution, distributions
from pathlib import Path
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def wheel_roots():
    """Yield packages installed from explicit wheel URLs or local wheel files."""
    # The base installs its GPU wheels from explicit URLs and local wheel files.
    # pip records both in PEP 610 metadata; no second list of wheel names to maintain.
    for dist in distributions():
        origin = dist.read_text("direct_url.json")
        if origin and urlsplit(json.loads(origin)["url"]).path.lower().endswith(".whl"):
            yield dist.metadata["Name"]


def wheel_pins(roots, lookup=distribution):
    """Pin base wheels and their active transitive requirements, including extras."""
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


def core_pins(requirements, names=("comfy-kitchen", "comfyui-frontend-package")):
    """Require active exact pins for protected packages while leaving other lines to pip."""
    pins = {}
    for line in re.sub(r"\\\r?\n", "", requirements).splitlines():
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        # Only inspect the protected packages. Other lines belong to pip and
        # may be options, includes, editable installs, or archive paths/URLs.
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if not match or canonicalize_name(match[0]) not in names:
            continue
        req = Requirement(line)
        name = canonicalize_name(req.name)
        if req.marker and not req.marker.evaluate():
            continue
        specs = list(req.specifier)
        if len(specs) == 1 and specs[0].operator == "==" and "*" not in specs[0].version:
            pins[name] = f"{name}=={specs[0].version}"
    for name in names:
        if name not in pins:
            raise RuntimeError(f"Upstream ComfyUI must pin {name}; review its requirements")
        yield pins[name]


def build_constraints(requirements, manager_requirements):
    """Combine the installed wheel closure with the selected release's core and Manager pins."""
    return [*wheel_pins(wheel_roots()), *core_pins(requirements),
            *core_pins(manager_requirements, ("comfyui-manager",))]


if __name__ == "__main__":
    print(*build_constraints(Path(sys.argv[1]).read_text(), Path(sys.argv[2]).read_text()), sep="\n")
