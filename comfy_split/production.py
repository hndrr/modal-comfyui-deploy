"""Production MCP operations use the split journal and named Volume roots, never client paths or URLs."""

import asyncio
import hashlib
import json
import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from aiohttp import web
from PIL import Image

from comfy_split import input_media as media
from comfy_split.storage import MOUNTS

KINDS = {
    **dict.fromkeys(("png", "jpeg", "jpg", "webp"), "image"),
    **dict.fromkeys(("wav", "flac", "ogg", "opus", "mp3", "m4a"), "audio"),
    **dict.fromkeys(("mp4", "mov", "mkv", "webm"), "video"),
}


def require(ok, reason):
    if not ok:
        raise web.HTTPBadRequest(text=reason)


def parts(path):
    require(isinstance(path, str) and 0 < len(path) <= 1024, "Invalid relative name")
    items = path.split("/")
    require(
        all(
            x
            and len(x) <= 255
            and not x.startswith(".")
            and not any(c in "\\:" or ord(c) < 32 or ord(c) == 127 for c in x)
            for x in items
        ),
        "Invalid relative name",
    )
    return items


@contextmanager
def regular(root, name):
    items = parts(name)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in items[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        f = os.open(items[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(f, "rb") as file:
            require(stat.S_ISREG(os.fstat(f).st_mode), "Expected a regular file")
            yield file
    finally:
        os.close(fd)


def page(rows, args):
    offset, limit = args.get("offset", 0), args.get("limit", 50)
    require(
        type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, "Invalid page"
    )
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
    }


def outputs(job):
    rows = []
    for node_id, node in (job.get("history") or {}).get("outputs", {}).items():
        if not isinstance(node, dict):
            continue
        for entries in node.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "output":
                    continue
                filename, sub = entry.get("filename"), entry.get("subfolder") or ""
                if not isinstance(filename, str) or "/" in filename or not isinstance(sub, str):
                    continue
                name = (sub + "/" if sub else "") + filename
                try:
                    parts(name)
                except web.HTTPBadRequest:
                    continue
                kind = KINDS.get(Path(filename).suffix.lower()[1:])
                if kind:
                    rows.append(
                        {
                            "index": len(rows),
                            "node_id": node_id,
                            "filename": filename,
                            "subfolder": sub,
                            "kind": kind,
                        }
                    )
    return rows


def inspect(file, name):
    ext = Path(name).suffix.lower()[1:]
    require(ext in KINDS, "Unsupported media")
    result = {"filename": name, "kind": KINDS[ext], "size_bytes": os.fstat(file.fileno()).st_size}
    if result["kind"] == "image":
        with Image.open(file) as image:
            result.update(width=image.width, height=image.height, format=image.format)
        return result
    demux = {
        "mov": "mov",
        "mp4": "mov",
        "m4a": "mov",
        "webm": "matroska",
        "mkv": "matroska",
        "opus": "ogg",
    }.get(ext, ext)
    fd = file.fileno()
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-f",
            demux,
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,sample_rate,channels",
            "-of",
            "json",
            f"/dev/fd/{fd}",
        ],
        pass_fds=(fd,),
        capture_output=True,
        timeout=15,
    )
    require(probe.returncode == 0, "Unreadable media")
    metadata = json.loads(probe.stdout)
    duration = metadata.get("format", {}).get("duration")
    result.update(
        duration_seconds=float(duration) if duration and duration != "N/A" else None,
        streams=metadata.get("streams", []),
    )
    return result


def production(name, a, jobs):
    root = Path(MOUNTS["input"])
    if name == "list_inputs":
        kind, query = a.get("kind"), a.get("query", "")
        require(kind is None or kind in KINDS.values(), "Unknown media kind")
        require(isinstance(query, str), "Invalid search")
        rows = []
        with os.scandir(root) as entries:
            for e in entries:
                k = KINDS.get(Path(e.name).suffix.lower()[1:])
                if (
                    k
                    and (not kind or k == kind)
                    and not e.name.startswith(".")
                    and query.casefold() in e.name.casefold()
                    and e.is_file(follow_symlinks=False)
                ):
                    rows.append(
                        {
                            "filename": e.name,
                            "kind": k,
                            "size_bytes": e.stat(follow_symlinks=False).st_size,
                        }
                    )
        return page(sorted(rows, key=lambda x: x["filename"]), a)
    if name == "inspect_media" and "filename" in a:
        require(not ({"index", "prompt_id"} & a.keys()), "Choose input or job output")
        with regular(root, a["filename"]) as file:
            return inspect(file, a["filename"])
    job_id = str(UUID(a.get("prompt_id", "")))
    job = jobs.get(job_id)
    if not job:
        raise web.HTTPNotFound(text="Job not found or its retention period ended")
    if name == "get_job_workflow":
        return {"prompt_id": job_id, "workflow": job["body"]["prompt"], "status": job["status"]}
    rows = outputs(job)
    if name == "list_outputs":
        return {"prompt_id": job_id, "status": job["status"], **page(rows, a)}
    require(job["status"] == "completed", "Job must be completed")
    index = a.get("index")
    require(type(index) is int and 0 <= index < len(rows), "No such output index; use list_outputs")
    item = rows[index]
    path = "/".join(filter(None, [item["subfolder"], item["filename"]]))
    with regular(MOUNTS["output"], path) as file:
        if name == "inspect_media":
            return inspect(file, path)
        require(name == "reuse_output", "Unknown operation")
        size = os.fstat(file.fileno()).st_size
        require(0 < size <= media.MAX, "Output exceeds the input size limit (256 MiB)")
        key = hashlib.sha256(f"reuse:{job_id}:{index}:{path}".encode()).hexdigest()
        t = media.Transfers(root, key)
        r = t.apply("status", item["filename"])
        if not r["complete"]:
            file.seek(r["next_offset"])
            while block := file.read(media.PART):
                r = t.apply("part", offset=r["next_offset"], data=block)
            r = t.apply("finish", total=size)
        return {"prompt_id": job_id, "index": index, "kind": item["kind"], "filename": r["filename"]}


async def handle(control, name, args):
    from copy import deepcopy

    job_id = args.get("prompt_id")
    jobs = (
        {job_id: deepcopy(control.journal.data["jobs"][job_id])}
        if isinstance(job_id, str) and job_id in control.journal.data["jobs"]
        else {}
    )
    try:
        if name == "reuse_output":
            async with media._lock:
                result = await asyncio.to_thread(production, name, args, jobs)
                await control.volumes["input"].commit.aio()
        else:
            result = await asyncio.to_thread(production, name, args, jobs)
        return result
    except OSError as exc:
        raise web.HTTPNotFound(text="File not found or not a regular media file") from exc
