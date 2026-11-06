"""Choose ComfyUI once on the deploying machine; containers reuse the image revision."""

import json
import os
import re
from urllib.parse import quote
from urllib.request import Request, urlopen


REVISION_ENV = "SPLIT_COMFYUI_REVISION"
API = "https://api.github.com/repos/Comfy-Org/ComfyUI"


def github_json(path):
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{API}/{path}", headers=headers)
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def comfy_revision(*, local):
    sha = os.environ.get(REVISION_ENV, "").strip()
    if local and not sha:
        try:
            release = github_json("releases/latest")
            tag = release["tag_name"]
            if release["draft"] or release["prerelease"] or not tag:
                raise ValueError("expected a published stable release")
            # target_commitish can be a branch. Resolve the tag as a commit,
            # including when the tag is annotated.
            ref = quote(f"refs/tags/{tag}", safe="")
            sha = github_json(f"commits/{ref}")["sha"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                "Cannot resolve the latest stable ComfyUI release; check GitHub access/token "
                f"or set {REVISION_ENV} to a known commit SHA"
            ) from exc
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Missing or invalid split ComfyUI commit SHA")
    if local:
        print(f"Split ComfyUI revision: {sha}", flush=True)
    return sha
