"""Resumable input transfers. Only validated media receives a loader-visible filename."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from aiohttp import web
from PIL import Image, UnidentifiedImageError

from comfy_split.storage import MOUNTS

MAX = 256 * 1024**2
PART = 8 * 1024**2
EXTENSIONS = {
    "png", "jpg", "jpeg", "webp", "wav", "mp3", "m4a", "ogg", "opus", "flac",
    "mp4", "mov", "webm", "mkv",
}
_lock = asyncio.Lock()


def invalid(message):
    raise web.HTTPBadRequest(text=message)


def detect(path):
    with path.open("rb") as f:
        head = f.read(4096)
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")) or head[8:12] == b"WEBP":
        try:
            with Image.open(path) as im:
                kind = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[im.format]
                im.verify()
            return kind
        except (KeyError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
            invalid("Invalid image")
    # Force a media demuxer. Never let ffprobe interpret playlists, URLs or manifests.
    if head.startswith(b"fLaC"):
        fmt = ext = "flac"
    elif head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE":
        fmt = ext = "wav"
    elif head.startswith(b"OggS"):
        fmt = ext = "ogg"
    elif head.startswith(b"ID3") or (len(head) > 1 and head[0] == 255 and head[1] & 224 == 224):
        fmt = ext = "mp3"
    elif head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide"):
        fmt, ext = "mov", "mov" if head[4:8] != b"ftyp" or head[8:12] == b"qt  " else "mp4"
    elif head.startswith(b"\x1aE\xdf\xa3") and (b"webm" in head or b"matroska" in head):
        fmt, ext = "matroska", "webm" if b"webm" in head else "mkv"
    else:
        invalid("Unsupported media signature")
    args = ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", fmt,
            "-show_entries", "stream=codec_type,codec_name:stream_disposition=attached_pic",
            "-of", "json", str(path)]
    try:
        result = subprocess.run(args, capture_output=True, timeout=15, check=True)
        streams = json.loads(result.stdout)["streams"]
    except (subprocess.SubprocessError, ValueError, KeyError):
        invalid("Unreadable media")
    audio = any(s.get("codec_type") == "audio" and s.get("codec_name") for s in streams)
    video = any(s.get("codec_type") == "video" and s.get("codec_name") and
                not s.get("disposition", {}).get("attached_pic") for s in streams)
    if fmt == "mov" and audio and not video:
        return "m4a"
    if fmt == "ogg" and audio and not video and any(s.get("codec_name") == "opus" for s in streams):
        return "opus"
    if (ext in {"mp4", "mov", "webm", "mkv"} and video) or (ext in {"wav", "mp3", "flac", "ogg"} and audio and not video):
        return ext
    invalid("No supported media stream")


class Transfers:
    def __init__(self, root, upload_id):
        if not re.fullmatch("[0-9a-f]{64}", upload_id):
            invalid("Invalid transfer ID")
        self.root = root
        stage = root / ".mcp-transfers"
        if stage.is_symlink():
            invalid("Invalid staging directory")
        stage.mkdir(exist_ok=True)
        self.dir = stage / upload_id
        if self.dir.is_symlink():
            invalid("Invalid transfer directory")
        self.id = upload_id
        self.part = self.dir / "data.part"
        self.meta = self.dir / "state.json"
        if any(p.is_symlink() for p in (self.meta, self.meta.with_suffix(".tmp"), self.part)):
            invalid("Invalid staging file")

    def save(self, meta):
        tmp = self.meta.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, self.meta)

    def publish(self, meta):
        # Modal Volumes support symlinks but not hard links or RENAME_NOREPLACE.
        # Exclusively reserve the loader name with a link to already validated
        # bytes, then atomically turn our own reservation into a regular file.
        filename = meta["publishing"]
        dest = self.root / filename
        relative = str(self.part.relative_to(self.root))
        try:
            dest.symlink_to(relative)
        except FileExistsError:
            pass
        if dest.is_symlink():
            if os.readlink(dest) != relative:
                raise web.HTTPConflict(text="Conflicting input filename")
            os.replace(self.part, dest)
        else:
            with dest.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != meta["digest"]:
                    raise web.HTTPConflict(text="Conflicting input filename")
        self.save({"name":meta["name"], "filename":filename, "size":meta["size"]})
        self.part.unlink(missing_ok=True)

    def apply(self, op, name="", offset=0, data=b"", total=None):
        if not self.meta.exists():
            if op != "status":
                invalid("Start or resume the transfer first")
            if not name or len(name) > 220 or any(c in name for c in "/\\") or any(ord(c)<32 for c in name):
                invalid("Invalid filename")
            suffix = Path(name).suffix.lower().lstrip(".")
            if suffix and suffix not in EXTENSIONS:
                invalid("Unsupported filename extension")
            # Cleanup on demand; no timer or container keep-alive.
            now = time.time()
            pending = 0
            for entry in self.dir.parent.iterdir():
                if re.fullmatch("[0-9a-f]{64}", entry.name) and not entry.is_symlink() and entry.is_dir() and now-entry.stat().st_mtime > 86400:
                    state = entry / "state.json"
                    if state.is_file() and "publishing" in json.loads(state.read_text()):
                        continue  # A validated publication interrupted before its receipt.
                    shutil.rmtree(entry)
                elif (entry / "data.part").is_file():
                    pending += 1
            if pending >= 16:
                raise web.HTTPTooManyRequests(text="Too many unfinished transfers")
            self.dir.mkdir(exist_ok=True)
            self.part.touch(exist_ok=False)
            self.save({"name": name})
        meta = json.loads(self.meta.read_text())
        if op == "status" and name != meta["name"]:
            raise web.HTTPConflict(text="Transfer ID is already used for another filename")
        if "publishing" in meta:
            self.publish(meta)
            meta = json.loads(self.meta.read_text())
        if "filename" in meta:
            if op == "part":
                with (self.root / meta["filename"]).open("rb") as f:
                    f.seek(offset)
                    if not data or f.read(len(data)) != data:
                        raise web.HTTPConflict(text="Replayed bytes differ from completed input")
            if op == "finish" and total != meta["size"]:
                raise web.HTTPConflict(text="Completed upload has a different size")
            return {"upload_id": self.id, "next_offset": meta["size"], "filename": meta["filename"], "complete": True}
        if self.part.is_symlink():
            invalid("Invalid staging file")
        size = self.part.stat().st_size
        if op == "part":
            if offset < 0 or not data or len(data) > PART or offset + len(data) > MAX:
                invalid("Invalid part or 256 MiB limit exceeded")
            with self.part.open("r+b") as f:
                f.seek(offset)
                if offset == size:
                    f.write(data)
                    size += len(data)
                elif offset > size or offset + len(data) > size or f.read(len(data)) != data:
                    raise web.HTTPConflict(text="Offset or replayed bytes differ; resume from status")
            os.utime(self.dir)
        elif op == "finish":
            if size != total or not 0 < size <= MAX:
                invalid("Incomplete or oversized file")
            ext = detect(self.part)
            suffix = Path(meta["name"]).suffix.lower().lstrip(".")
            if suffix and suffix != ext and (suffix, ext) not in {("jpeg","jpg"), ("ogg","opus")}:
                invalid("Filename extension does not match the content")
            # Hashing also makes a lost finish receipt safe to retry after a restart.
            with self.part.open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            filename = f"mcp-{digest}.{ext}"
            pending = {**meta, "publishing":filename, "digest":digest, "size":size}
            self.save(pending)
            self.publish(pending)
            return {"upload_id": self.id, "next_offset": size, "filename": filename, "complete": True}
        elif op != "status":
            invalid("Unknown transfer operation")
        return {"upload_id": self.id, "next_offset": size, "complete": False}


async def handle(request, volume):
    op = request.query.get("op")
    upload_id = request.query.get("id", "")
    offset = request.query.get("offset", "0")
    total = request.query.get("total", "0")
    if not offset.isascii() or not offset.isdigit() or not total.isascii() or not total.isdigit() or max(len(offset),len(total)) > 10:
        invalid("Invalid offset or size")
    data = bytearray()
    async for block in request.content.iter_chunked(65536):
        if op != "part" or len(data) + len(block) > PART:
            invalid("Oversized request")
        data.extend(block)
    async with _lock:
        def run():
            return Transfers(Path(MOUNTS["input"]), upload_id).apply(
                op, request.query.get("name", ""), int(offset), data, int(total))
        result = await asyncio.to_thread(run)
        await volume.commit.aio()
    return web.json_response(result)
