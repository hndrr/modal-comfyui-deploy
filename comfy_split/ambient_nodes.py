"""Refresh Ambient's private node packs without mutating an active environment."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from comfy_split.state import write_json

MODE_ENV = "COMFYUI_AMBIENT_MODE"
TOKEN_ENV = "GITHUB_TOKEN"
REPOSITORIES = (
    "hndrr/ComfyUI-AgentRuntime",
    "hndrr/ComfyUI-Skills-Loader",
    "hndrr/ComfyUI-GeminiTools",
    "hndrr/ComfyUI-Jev",
)
NODE_NAMES = frozenset(repo.split("/")[1] for repo in REPOSITORIES)
DIRECTORY = "ambient_nodes"
MANIFEST = "ambient-nodes.json"


def enabled(environ=None):
    environ = os.environ if environ is None else environ
    value = environ.get(MODE_ENV, "off").strip().lower()
    if value not in {"on", "off"}:
        raise ValueError(f"{MODE_ENV} must be on or off")
    return value == "on"


def is_ambient_node(definition):
    return definition.get("python_module", "").removeprefix("custom_nodes.") in NODE_NAMES


def check_catalog(catalog):
    loaded = {definition.get("python_module", "").removeprefix("custom_nodes.")
              for definition in catalog["objects"].values()}
    missing = NODE_NAMES - loaded
    if missing:
        raise RuntimeError("Ambient nodes failed to load: " + ", ".join(sorted(missing)))


def prepare_environment(source):
    """CPU startup only. Return a new environment, or None when already current.

    Clone each default branch afresh: no credentials in remotes, no force-pull
    over user edits, and all four downloads must succeed before copying a venv.
    The caller validates CPU imports and commits before publishing the version.
    """
    from comfy_split.runtime import create_environment, environment_path

    if not os.environ.get(TOKEN_ENV):
        raise RuntimeError(f"Modal Secret must supply {TOKEN_ENV} for private Ambient repositories")
    origin = environment_path(source)
    with tempfile.TemporaryDirectory(prefix="ambient-nodes-") as directory:
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
        for repo in REPOSITORIES:
            name = repo.split("/")[1]
            destination = staging / name
            subprocess.run(
                ["git", "-c", "credential.helper=", "clone", "--quiet", "--depth", "1",
                 "https://github.com/" + repo + ".git", str(destination)],
                env=git_env, check=True, timeout=60,
            )
            revisions[name] = subprocess.check_output(
                ["git", "-C", str(destination), "rev-parse", "HEAD"],
                env=git_env, text=True, timeout=10,
            ).strip()
        # A duplicate in the user's node directory makes import precedence
        # ambiguous. Preserve it and report the conflict instead of replacing it.
        duplicates = [name for name in NODE_NAMES
                      if (origin / "comfy/custom_nodes" / name).exists()]
        if duplicates:
            raise RuntimeError("Ambient nodes already installed in custom_nodes: "
                               + ", ".join(sorted(duplicates)))
        previous = origin / MANIFEST
        if (previous.exists() and json.loads(previous.read_text()) == revisions
                and all((origin / DIRECTORY / name / "__init__.py").is_file() for name in NODE_NAMES)):
            return None
        version = create_environment(source)
        target = environment_path(version)
        nodes = target / DIRECTORY
        if nodes.exists():
            shutil.rmtree(nodes)
        nodes.mkdir()
        for name in revisions:
            shutil.move(str(staging / name), nodes / name)
        python = str(target / "venv/bin/python")
        requirements = [node / "requirements.txt" for node in sorted(nodes.iterdir())
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
        # Only Ambient changed. Preserve definitions of unrelated GPU-only nodes.
        if (origin / "catalog.json").is_file():
            shutil.copyfile(origin / "catalog.json", target / "catalog.json")
        return version
