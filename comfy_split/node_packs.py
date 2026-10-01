"""Optional managed node packs, independent of any gateway extension."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from comfy_split.state import write_json
from comfy_split.extension_sources import LEGACY, node_revision

from comfy_split.config import Settings, NODE_PACKS, DEPLOYMENT_ENV

TOKEN_ENV = "GITHUB_TOKEN"
REFRESH_KEY = "node_packs_refresh"
REPOSITORIES = tuple(NODE_PACKS.values())
NODE_NAMES = frozenset(repo.split("/")[1] for repo in REPOSITORIES)
DIRECTORY = "node_packs"
MANIFEST = "node-packs.json"
LEGACY_DIRECTORY = LEGACY["node_directory"]
LEGACY_MANIFEST = LEGACY["node_manifest"]


def enabled():
    return bool(Settings.read().node_packs)


def repositories():
    return tuple(NODE_PACKS[name] for name in Settings.read().node_packs)


def node_names():
    return frozenset(repo.split("/")[1] for repo in repositories())


def node_path(origin, name):
    current = origin / DIRECTORY / name
    return current if current.is_dir() else origin / LEGACY_DIRECTORY / name


def is_managed_node(definition):
    return definition.get("python_module", "").removeprefix("custom_nodes.") in NODE_NAMES


def check_catalog(catalog):
    loaded = {definition.get("python_module", "").removeprefix("custom_nodes.")
              for definition in catalog["objects"].values()}
    missing = node_names() - loaded
    if missing:
        raise RuntimeError("Managed nodes failed to load: " + ", ".join(sorted(missing)))


def snapshot_revisions(source):
    """Read the saved snapshot locally; ordinary starts need no GitHub access."""
    from comfy_split.runtime import environment_path

    origin = environment_path(source)
    try:
        manifest = origin / MANIFEST
        if not manifest.exists():
            manifest = origin / LEGACY_MANIFEST
        revisions = json.loads(manifest.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if (not isinstance(revisions, dict) or not node_names() <= set(revisions)
            or any(not isinstance(sha, str) or len(sha) != 40
                   or any(c not in "0123456789abcdef" for c in sha)
                   for sha in revisions.values())
            or not all((node_path(origin, name) / "__init__.py").is_file()
                       for name in node_names())):
        return None
    return {name: revisions[name] for name in node_names()}


def prepare_environment(source):
    """Deployment update only. Return a new snapshot, or None when current.

    Resolve explicit pins or default-branch HEADs, then fetch only changed packs.
    No credentials in remotes, no force-pull over user edits, and every required
    download must succeed before copying a venv.
    The caller validates CPU imports and commits before publishing the version.
    """
    from comfy_split.runtime import create_environment, environment_path

    if not os.environ.get(TOKEN_ENV):
        raise RuntimeError(f"Modal Secret must supply {TOKEN_ENV} for private managed node repositories")
    origin = environment_path(source)
    previous = snapshot_revisions(source) or {}
    # Preserve user-installed duplicates instead of silently changing precedence.
    duplicates = [name for name in node_names()
                  if (origin / "comfy/custom_nodes" / name).exists()]
    if duplicates:
        raise RuntimeError("Managed nodes already installed in custom_nodes: "
                           + ", ".join(sorted(duplicates)))
    with tempfile.TemporaryDirectory(prefix="split-nodes-") as directory:
        staging = Path(directory)
        askpass = staging / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in\n'
            '  *Username*) printf "%s\\n" "x-access-token" ;;\n'
            f'  *) printf "%s\\n" "${TOKEN_ENV}" ;;\n'
            'esac\n'
        )
        askpass.chmod(0o700)
        git_env = dict(os.environ, GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0")
        # Never carry inherited Git HTTP tracing into authenticated fetches.
        for key in list(git_env):
            if key.startswith("GIT_TRACE") or key == "GIT_CURL_VERBOSE":
                git_env.pop(key)
        revisions = {}
        for repo in repositories():
            name = repo.split("/")[1]
            pinned = node_revision(repo)
            if pinned:
                revisions[name] = pinned
                continue
            head = subprocess.check_output(
                ["git", "-c", "credential.helper=", "ls-remote", "--exit-code",
                 "https://github.com/" + repo + ".git", "HEAD"],
                env=git_env, text=True, timeout=60,
            ).split()
            if (len(head) != 2 or head[1] != "HEAD" or len(head[0]) != 40
                    or any(c not in "0123456789abcdef" for c in head[0])):
                raise RuntimeError("Invalid default-branch revision for " + repo)
            revisions[name] = head[0]
        if previous == revisions:
            return None
        changed = {name for name in revisions if previous.get(name) != revisions[name]}
        for repo in repositories():
            name = repo.split("/")[1]
            if name not in changed:
                continue
            destination = staging / name
            subprocess.run(
                ["git", "init", "--quiet", str(destination)],
                env=git_env, check=True, timeout=60,
            )
            subprocess.run(
                ["git", "-c", "credential.helper=", "-C", str(destination),
                 "fetch", "--quiet", "--depth", "1", "https://github.com/" + repo + ".git",
                 revisions[name]], env=git_env, check=True, timeout=60,
            )
            subprocess.run(
                ["git", "-C", str(destination), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
                env=git_env, check=True, timeout=10,
            )
        # Validate the selected combination before creating or publishing it.
        selected = [staging / name if name in changed else node_path(origin, name)
                    for name in revisions]
        check_compatibility([*selected, origin / "comfy/custom_nodes"])
        version = create_environment(source)
        target = environment_path(version)
        nodes = target / DIRECTORY
        nodes.mkdir(exist_ok=True)
        for name in revisions.keys() - changed:
            if not (nodes / name).is_dir():
                shutil.copytree(node_path(origin, name), nodes / name, symlinks=True)
        for name in changed:
            if (nodes / name).exists():
                shutil.rmtree(nodes / name)
            shutil.move(str(staging / name), nodes / name)
        python = str(target / "venv/bin/python")
        requirements = [node / "requirements.txt" for node in sorted(nodes / name for name in revisions)
                        if (node / "requirements.txt").is_file()]
        # The GitHub token is not needed by pip or ComfyUI's dependency checker.
        dependency_env = dict(os.environ)
        dependency_env.pop(TOKEN_ENV, None)
        if requirements:
            subprocess.run(
                [python, "-m", "pip", "install", "-c", "/opt/split-constraints.txt",
                 *[arg for path in requirements for arg in ("-r", str(path))]],
                env=dependency_env, check=True, timeout=240,
            )
        subprocess.run([python, "-m", "comfy_split.check_environment"],
                       env=dependency_env, check=True, timeout=60)
        write_json(target / MANIFEST, revisions)
        # Only managed packs changed. Preserve definitions of unrelated GPU-only nodes.
        if (origin / "catalog.json").is_file():
            shutil.copyfile(origin / "catalog.json", target / "catalog.json")
        return version


def check_compatibility(roots):
    """Reject duplicate Bridge implementations without importing either package.

    Only enabled/searchable paths are passed here; older saved environments and
    disabled packs are not inspected or changed.
    """
    paths = []
    for root in map(Path, roots):
        if root.is_dir():
            paths.extend([root, *[p for p in root.iterdir() if p.is_dir()]])
    standalone = any((p / "comfyui_agent_bridge/bridge/nodes.py").is_file() for p in paths)
    legacy = any((p / "comfyui_agent_runtime/bridge/nodes.py").is_file() for p in paths)
    if standalone and legacy:
        raise RuntimeError("Update ComfyUI-AgentRuntime together with ComfyUI-AgentBridge; duplicate Bridge nodes cannot be loaded.")
