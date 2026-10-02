"""Split storage layout and conservative retention policy (no Modal calls on import)."""

import json
import time
from pathlib import Path

from comfy_split.state import ACTIVE

VOLUME_NAMES = {
    "models": "comfy-model", "input": "comfy-inputs", "output": "comfy-outputs",
    "data": "comfy-split-data", "seed_user": "comfy-user-data", "nodes": "comfy-custom-nodes",
    "environment": "comfy-split-environments",
}
MOUNTS = {"models": "/models", "input": "/data/input", "output": "/data/output",
          "data": "/split-data", "nodes": "/data/custom_nodes",
          "environment": "/environments", "seed_user": "/seed/user"}
DATA = Path(MOUNTS["data"])
USER = DATA / "user"
STATE = DATA / "state"
JOBS = DATA / "jobs"
ENVIRONMENTS = Path(MOUNTS["environment"])
TEMP_ARCHIVE = Path(MOUNTS["output"]) / ".split-temp"
HISTORY_SECONDS = 7 * 86400
TEMP_SECONDS = 86400
TERMINAL = {"completed", "failed", "cancelled"}


def expired_jobs(data, now):
    """Missing completion times and ambiguous executions are never aged out."""
    return [key for key, job in data["jobs"].items()
            if job["status"] in TERMINAL and job.get("finished_at") is not None
            and job["finished_at"] <= now - HISTORY_SECONDS]


def protected_environments(data):
    protected = {"base": "initial", data["environment"]: "active"}
    for record in [data.get("candidate"), data.get("session")]:
        if record:
            version = record.get("version") or record.get("environment")
            if version:
                protected[version] = "candidate/session"
    for job in data["jobs"].values():
        if job["status"] in ACTIVE and job.get("environment"):
            protected[job["environment"]] = "unfinished job"
    return protected


def temp_references(value):
    """Return archive-relative paths; unknown/custom payloads stay conservative."""
    found = set()
    if isinstance(value, list):
        for item in value:
            found.update(temp_references(item))
    elif isinstance(value, dict):
        filename = value.get("filename")
        if isinstance(filename, str):
            subfolder = value.get("subfolder", "")
            if isinstance(subfolder, str):
                relative = Path(subfolder) / filename
                if not relative.is_absolute() and ".." not in relative.parts:
                    if value.get("type") == "temp":
                        found.add((Path("temp") / relative).as_posix())
                    elif value.get("type") == "output" and relative.parts[:1] == (".split-temp",):
                        found.add(Path(*relative.parts[1:]).as_posix())
        for item in value.values():
            found.update(temp_references(item))
    return found


def cleanup_plan(data, environment_names, receipts, temporary_files, *, now=None,
                 live_temp_namespaces=(), snapshot_environments=()):
    """Determine eligible files without performing deletions.

    Receipt entries contain path/mtime/result; temp entries contain path/mtime.
    Deleting a start receipt requires a matching terminal result OR a durable
    terminal CPU record. The caller must additionally verify the GPU is stopped.
    """
    now = time.time() if now is None else now
    expired = set(expired_jobs(data, now))
    protected = protected_environments(data)
    for version in snapshot_environments:
        protected.setdefault(version, "CPU memory snapshot")
    references = temp_references([job for key, job in data["jobs"].items() if key not in expired])
    references.update(temp_references(data.get("session")))
    protected_jobs = {key for key, job in data["jobs"].items() if key not in expired}
    if data.get("session"):
        protected_jobs.add(data["session"]["id"])
    receipt_map = {entry["path"]: entry for entry in receipts}
    finished = expired | set(data.get("retired_jobs", {}))
    finished.update(entry["path"][:-5] for entry in receipts
                    if not entry["path"].endswith(".started.json")
                    and entry["result"].get("status") in TERMINAL
                    and entry["mtime"] <= now - HISTORY_SECONDS)
    removable = []
    for entry in receipts:
        name = entry["path"]
        if "/" in name or not name.endswith(".json"):
            continue
        job_id = name.removesuffix(".started.json") if name.endswith(".started.json") else name[:-5]
        if job_id not in protected_jobs and job_id in finished:
            removable.append(name)
    # Orphan unknown/start-only records are retained, along with their temp
    # references. With no durable owner, it is unsafe to infer they are finished.
    unknown_receipt = False
    for entry in receipts:
        if entry["path"] not in removable:
            result = entry.get("result", {})
            references.update(temp_references(result))
            if entry["path"].endswith(".started.json"):
                job_id = entry["path"].removesuffix(".started.json")
                result = receipt_map.get(job_id + ".json", {}).get("result", {})
                if job_id not in data["jobs"] and job_id not in finished and result.get("status") not in TERMINAL:
                    unknown_receipt = True
    temp = []
    if not unknown_receipt and not any(j["status"] in ACTIVE for j in data["jobs"].values()) and not data.get("session"):
        for entry in temporary_files:
            path = Path(entry["path"])
            if path.is_absolute() or ".." in path.parts or not path.parts:
                continue
            if (entry["mtime"] <= now - TEMP_SECONDS and entry["path"] not in references
                    and path.parts[0] not in live_temp_namespaces):
                temp.append(entry["path"])
    return {"expired_jobs": sorted(expired), "environments": sorted(
                name for name in environment_names if not unknown_receipt and name not in protected
                and name.startswith("env-") and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name)),
            "protected_environments": protected, "unresolved_receipts": unknown_receipt,
            "receipts": sorted(removable), "temporary_files": sorted(temp)}


async def read_json(volume, path):
    content = b"".join([chunk async for chunk in volume.read_file.aio(path)])
    return json.loads(content)


async def snapshot_environments(volume):
    """Pins outlive journal history: Volume edits do not invalidate Modal snapshots.

    Fail closed on unreadable pins. Retire pins only after the corresponding
    deployment/snapshots can no longer be restored (including rollbacks).
    """
    versions = set()
    try:
        entries = [entry async for entry in volume.iterdir.aio("/.cpu-snapshots", recursive=False)]
    except FileNotFoundError:
        return versions
    for entry in entries:
        if entry.path.endswith(".json"):
            pin = await read_json(volume, entry.path)
            versions.add(pin["environment"])
    return versions


async def remote_receipts(volume):
    entries = []
    try:
        async for entry in volume.iterdir.aio("/jobs", recursive=False):
            path = Path(entry.path)
            if path.suffix != ".json":
                continue
            try:
                result = await read_json(volume, entry.path)
            except (ValueError, FileNotFoundError):
                result = {}
            result = result if isinstance(result, dict) else {}
            entries.append({"path": path.name, "mtime": result.get("finished_at", entry.mtime), "result": result})
    except FileNotFoundError:
        pass
    return entries


async def remote_files(volume, root):
    entries = []
    try:
        async for entry in volume.iterdir.aio(root, recursive=True):
            # FileEntryType.FILE == 1; never traverse or delete symlink targets.
            if entry.type == 1:
                relative = Path(entry.path.lstrip("/")).relative_to(root.strip("/"))
                entries.append({"path": relative.as_posix(), "mtime": entry.mtime,
                                "size": entry.size})
    except FileNotFoundError:
        pass
    return entries
