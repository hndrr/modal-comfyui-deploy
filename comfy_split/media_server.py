"""Read saved outputs without importing or starting ComfyUI.

Volume reloads and bounded disk reads share a lock. No file descriptor remains
open while sending to a slow client, so another request can reload the Volume.
Each chunk verifies the original file version before it is sent.
"""

import asyncio
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

FORMATS = {
    "png": ("image", "image/png"), "jpg": ("image", "image/jpeg"),
    "jpeg": ("image", "image/jpeg"), "webp": ("image", "image/webp"),
    "mp3": ("audio", "audio/mpeg"), "wav": ("audio", "audio/wav"),
    "flac": ("audio", "audio/flac"), "ogg": ("audio", "audio/ogg"),
    "opus": ("audio", "audio/ogg"), "m4a": ("audio", "audio/mp4"),
    "mp4": ("video", "video/mp4"), "mov": ("video", "video/quicktime"),
    "webm": ("video", "video/webm"), "mkv": ("video", "video/x-matroska"),
}
BLOCK = 256 * 1024
JSON_LIMIT = 32 * 1024 * 1024


def parts(name):
    if not isinstance(name, str) or not 0 < len(name) <= 1280:
        raise ValueError("Invalid output name")
    result = name.split("/")
    if any(not p or p.startswith(".") or len(p) > 255 or
           any(c in "\\:" or ord(c) < 32 or ord(c) == 127 for c in p) for p in result):
        raise ValueError("Invalid output name")
    return result


@contextmanager
def regular(root, name):
    names = parts(name)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in names[:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(fd)
        fd = child
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FileNotFoundError("Not a regular file")
        yield fd
    finally:
        os.close(fd)


def version(info):
    # Modal's FUSE mount can assign a new inode after Volume.reload even when
    # the file is unchanged. Use persisted metadata, including ctime so an
    # overwrite with a restored mtime still interrupts the transfer.
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_json(root, name):
    with regular(root, name) as fd:
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(JSON_LIMIT + 1)
    if len(raw) > JSON_LIMIT:
        raise ValueError("Saved job exceeds the metadata read limit")
    return json.loads(raw)


def saved_job(root, prompt_id):
    if str(UUID(prompt_id)) != prompt_id:
        raise ValueError("Invalid prompt_id")
    try:
        return read_json(root, f"jobs/{prompt_id}.json")
    except FileNotFoundError:
        # Controller-created failures and pre-existing jobs may have no receipt.
        return read_json(root, "state/controller.json").get("jobs", {}).get(prompt_id)


def output_rows(job):
    result = []
    for node_id, node in (job.get("history") or {}).get("outputs", {}).items():
        if not isinstance(node, dict):
            continue
        for entries in node.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "output":
                    continue
                name, folder = entry.get("filename"), entry.get("subfolder") or ""
                try:
                    if len(parts(name)) != 1 or not isinstance(folder, str):
                        continue
                    parts((folder + "/" if folder else "") + name)
                except ValueError:
                    continue
                fmt = FORMATS.get(Path(name).suffix.lower()[1:])
                if fmt:
                    result.append(dict(index=len(result), node_id=node_id, filename=name,
                                       subfolder=folder, kind=fmt[0]))
    return result


def byte_range(value, size):
    if not value:
        return 0, size, 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()) or not size:
        raise ValueError("Unsatisfiable byte range")
    first, last = match.groups()
    start = int(first) if first else max(0, size - int(last))
    end = min(size, int(last) + 1) if first and last else size
    if start >= size or end <= start:
        raise ValueError("Unsatisfiable byte range")
    return start, end, 206


class MediaServer:
    def __init__(self, output, data, volumes=(), *, models=None):
        from comfy_split.saved_catalog import SavedCatalog
        self.output, self.data, self.volumes = Path(output), Path(data), volumes
        self.catalog = SavedCatalog(data, models)
        self.lock = asyncio.Lock()

    async def refresh(self):
        for volume in self.volumes:
            await volume.reload.aio()

    async def disk(self, function, *args):
        # Cancellation must not release the reload lock while a disk thread
        # still has a descriptor open.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        method = scope["method"]

        async def reply(status, payload, headers=None):
            body = json.dumps(payload, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", b"application/json"), (b"cache-control", b"private, no-store"),
                (b"content-length", str(len(body)).encode()), *(headers or [])]})
            await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})

        if method not in {"GET", "HEAD"}:
            return await reply(405, {"error": "Read-only endpoint"}, [(b"allow", b"GET, HEAD")])
        path = scope["path"]
        query = parse_qs(scope.get("query_string", b"").decode(), keep_blank_values=True)
        headers = dict(scope.get("headers", []))
        try:
            if any(len(v) != 1 for v in query.values()):
                raise ValueError("Duplicate parameters")
            q = {k: v[0] for k, v in query.items()}
            if path.startswith("/catalog/"):
                async with self.lock:
                    await self.refresh()
                    result = await self.disk(self.catalog.read, path, q)
                return await reply(200, result)
            if path.startswith("/history/") or path.startswith("/outputs/"):
                prompt_id = path.split("/", 2)[2]
                async with self.lock:
                    await self.refresh()
                    job = await self.disk(saved_job, self.data, prompt_id)
                if not job:
                    return await reply(404, {"error": "Job not found or history expired; no generation was submitted"})
                if path.startswith("/history/"):
                    history = job.get("history")
                    return await reply(200, {prompt_id: {k: history[k] for k in ("outputs", "status") if k in history}}
                                       if history else {})
                rows = output_rows(job)
                offset, limit = int(q.get("offset", "0")), int(q.get("limit", "50"))
                if offset < 0 or not 1 <= limit <= 100:
                    raise ValueError("Invalid page")
                return await reply(200, dict(prompt_id=prompt_id, status=job["status"],
                    items=rows[offset:offset + limit], total=len(rows),
                    next_offset=offset + limit if offset + limit < len(rows) else None))
            if path not in {"/view", "/api/view"}:
                return await reply(404, {"error": "Not found"})
            if q.get("type", "output") != "output" or "preview" in q or "channel" in q:
                raise ValueError("Only original saved outputs are available")
            name, folder = q.get("filename", ""), q.get("subfolder", "")
            if len(parts(name)) != 1:
                raise ValueError("Filename must be a basename")
            name = (folder + "/" if folder else "") + name
            parts(name)
            fmt = FORMATS.get(Path(name).suffix.lower()[1:])
            if not fmt:
                raise ValueError("Unsupported media format")

            def file_info():
                with regular(self.output, name) as fd:
                    return os.fstat(fd)

            async with self.lock:
                await self.refresh()
                info = await self.disk(file_info)
            original = version(info)
            etag = ('"' + "-".join(str(v) for v in original) + '"').encode()
            response_headers = [(b"content-type", fmt[1].encode()), (b"accept-ranges", b"bytes"),
                (b"etag", etag), (b"cache-control", b"private, no-store"),
                (b"x-content-type-options", b"nosniff"),
                (b"content-security-policy", b"default-src 'none'; sandbox")]
            if headers.get(b"if-none-match") in (etag, b"*"):
                await send({"type": "http.response.start", "status": 304, "headers": response_headers})
                return await send({"type": "http.response.body", "body": b""})
            requested = headers.get(b"range", b"").decode()
            if headers.get(b"if-range", etag) != etag:
                requested = ""
            try:
                start, end, status = byte_range(requested, info.st_size)
            except ValueError:
                return await reply(416, {"error": "Unsatisfiable byte range"},
                                   [(b"content-range", f"bytes */{info.st_size}".encode())])
            response_headers.append((b"content-length", str(end - start).encode()))
            if status == 206:
                response_headers.append((b"content-range", f"bytes {start}-{end-1}/{info.st_size}".encode()))
        except (ValueError, UnicodeError):
            return await reply(400, {"error": "Invalid media request or saved metadata"})
        except OSError:
            return await reply(404, {"error": "Saved media or job not found"})
        except Exception:
            return await reply(503, {"error": "Media storage temporarily unavailable; retry the read"},
                               [(b"retry-after", b"2")])

        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        if method == "HEAD":
            return await send({"type": "http.response.body", "body": b""})

        def read_chunk(offset):
            with regular(self.output, name) as fd:
                if version(os.fstat(fd)) != original:
                    raise OSError("Output changed during delivery; retry the read")
                return os.pread(fd, min(BLOCK, end - offset), offset)

        # Stop on client disconnect, including when ASGI send itself does not raise.
        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                pass

        disconnect = asyncio.create_task(disconnected())
        try:
            while start < end and not disconnect.done():
                async with self.lock:
                    chunk = await self.disk(read_chunk, start)
                if not chunk:
                    raise OSError("Incomplete saved output")
                start += len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": start < end})
            if info.st_size == 0:
                await send({"type": "http.response.body", "body": b""})
        finally:
            disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)
