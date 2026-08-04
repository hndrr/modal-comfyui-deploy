"""Long-lived JSONL RPC for Modal Volume ops (used by the Hono server).

Keeps Modal SDK warm. List pagination stays in Python so Node never
receives 20k-entry payloads on every page flip.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from asset_manager import (
    ALLOWED_VOLUMES,
    INPUT_VOLUME,
    MODEL_VOLUME,
    OUTPUT_VOLUME,
    AssetEntry,
    AssetManager,
    normalize_volume_path,
)

VOLUME_LABELS = {
    INPUT_VOLUME: "Inputs",
    OUTPUT_VOLUME: "Outputs",
    MODEL_VOLUME: "Models",
}
SORT_CHOICES = {
    "name_asc",
    "name_desc",
    "modified_desc",
    "modified_asc",
    "size_desc",
    "size_asc",
    "type_asc",
    "type_desc",
}
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 200
LIST_CACHE_TTL_SEC = 60.0
# Long edge for generated JPEG thumbnails (images + video posters).
THUMB_MAX_EDGE = max(64, min(1024, int(os.getenv("COMFY_ASSET_THUMB_MAX", "256"))))
THUMB_JPEG_QUALITY = max(40, min(95, int(os.getenv("COMFY_ASSET_THUMB_QUALITY", "80"))))
# Durable cache bounds (files/ + thumbs/). Override via env if needed.
CACHE_MAX_BYTES = max(
    64 * 1024 * 1024,
    int(os.getenv("COMFY_ASSET_CACHE_MAX_BYTES", str(8 * 1024 * 1024 * 1024))),
)
CACHE_MAX_FILES = max(64, int(os.getenv("COMFY_ASSET_CACHE_MAX_FILES", "4000")))
# Modal Volume delete is rate-limited / flaky under high concurrency.
# Keep this low (2–3). Override with ASSET_DELETE_WORKERS if needed.
DEFAULT_DELETE_WORKERS = max(1, min(8, int(os.getenv("ASSET_DELETE_WORKERS", "4"))))
DELETE_RETRIES = max(1, int(os.getenv("ASSET_DELETE_RETRIES", "3")))


def _default_cache_root() -> Path:
    """Persistent disk cache so restarts don't re-download every thumbnail."""
    env = os.getenv("COMFY_ASSET_CACHE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    xdg = os.getenv("XDG_CACHE_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "modal-comfyui-assets"
    return Path.home() / ".cache" / "modal-comfyui-assets"


MANAGER = AssetManager()
LOCK = threading.Lock()
STDOUT_LOCK = threading.Lock()
# Ephemeral workspace for non-cache scratch; file/thumb caches live under CACHE_ROOT.
WORKSPACE = Path(tempfile.mkdtemp(prefix="comfy-asset-rpc-"))
CACHE_ROOT = _default_cache_root()
CACHE_DIR = CACHE_ROOT / "files"
THUMB_DIR = CACHE_ROOT / "thumbs"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)

EmitFn = Callable[[dict[str, Any]], None]

# volume:path -> (monotonic expires, entries)
_list_cache: dict[str, tuple[float, list[AssetEntry]]] = {}
_thumb_lock = threading.Lock()
_thumb_inflight: dict[str, threading.Event] = {}


def _now() -> float:
    import time

    return time.monotonic()


def _record(entry: AssetEntry) -> dict[str, Any]:
    data = asdict(entry)
    data["modified_at"] = entry.modified_at.isoformat()
    data["is_directory"] = entry.is_directory
    return data


def _breadcrumb(path: str) -> list[dict[str, str]]:
    items = [{"label": "/", "path": ""}]
    parts: list[str] = []
    for part in PurePosixPath(path).parts if path else ():
        parts.append(part)
        value = PurePosixPath(*parts).as_posix()
        items.append({"label": part, "path": value})
    return items


def _sort_entries(entries: list[AssetEntry], sort_mode: str) -> list[AssetEntry]:
    if sort_mode not in SORT_CHOICES:
        sort_mode = "modified_desc"
    reverse = sort_mode.endswith("_desc")

    def sort_key(item: AssetEntry):
        if sort_mode.startswith("modified"):
            return item.modified_at
        if sort_mode.startswith("size"):
            return item.size
        if sort_mode.startswith("type"):
            # Extension first (e.g. .mp4 / .png), then name for stable groups.
            suffix = PurePosixPath(item.name).suffix.casefold()
            if item.is_directory:
                suffix = ""
            return (suffix, item.name.casefold())
        return item.name.casefold()

    sorted_entries = sorted(entries, key=sort_key, reverse=reverse)
    return sorted(sorted_entries, key=lambda item: not item.is_directory)


def _load_entries(volume: str, path: str, *, refresh: bool = False) -> list[AssetEntry]:
    key = f"{volume}:{path}"
    if not refresh:
        cached = _list_cache.get(key)
        if cached and cached[0] > _now():
            return list(cached[1])
    elif key in _list_cache:
        del _list_cache[key]
    entries = MANAGER.list_assets(volume, path)
    # Store a shallow copy so later in-place filtering never mutates the cache.
    _list_cache[key] = (_now() + LIST_CACHE_TTL_SEC, list(entries))
    return list(entries)


def _invalidate_list_cache(volume: str | None = None) -> None:
    if volume is None:
        _list_cache.clear()
        return
    prefix = f"{volume}:"
    for key in list(_list_cache):
        # Match "comfy-inputs:" (root) and "comfy-inputs:subdir/..."
        if key == volume or key.startswith(prefix):
            del _list_cache[key]


def _entry_digest(entry: AssetEntry, *, kind: str) -> str:
    payload = (
        f"{kind}:{entry.volume}:{entry.path}:"
        f"{entry.modified_at.isoformat()}:{entry.size}:{THUMB_MAX_EDGE}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cached_path(entry: AssetEntry) -> Path:
    digest = _entry_digest(entry, kind="file")
    return CACHE_DIR / f"{digest}{PurePosixPath(entry.path).suffix}"


def _thumb_path(entry: AssetEntry) -> Path:
    digest = _entry_digest(entry, kind="thumb")
    return THUMB_DIR / f"{digest}.jpg"


def _atomic_replace(tmp: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp.replace(destination)


def _evict_cache_if_needed() -> None:
    """Drop oldest cache files when over byte/count limits."""
    files: list[tuple[float, int, Path]] = []
    for root in (CACHE_DIR, THUMB_DIR):
        if not root.exists():
            continue
        for path in root.iterdir():
            if not path.is_file() or path.name.endswith(".tmp") or ".tmp." in path.name:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((stat.st_mtime, stat.st_size, path))
    if not files:
        return
    total_bytes = sum(size for _, size, _ in files)
    count = len(files)
    if total_bytes <= CACHE_MAX_BYTES and count <= CACHE_MAX_FILES:
        return
    files.sort(key=lambda item: item[0])  # oldest first
    for _mtime, size, path in files:
        if total_bytes <= CACHE_MAX_BYTES and count <= CACHE_MAX_FILES:
            break
        try:
            path.unlink(missing_ok=True)
            total_bytes -= size
            count -= 1
        except OSError:
            continue


def _materialize(entry: AssetEntry) -> dict[str, Any]:
    destination = _cached_path(entry)
    if not destination.exists() or destination.stat().st_size == 0:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{destination.name}.tmp.",
            dir=destination.parent,
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            MANAGER.download_listed_asset(entry, tmp)
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError(f"Download produced empty file for {entry.path}")
            _atomic_replace(tmp, destination)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        _evict_cache_if_needed()
    media_type = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
    return {
        "path": destination.as_posix(),
        "name": entry.name,
        "media_type": media_type,
        "size": destination.stat().st_size if destination.exists() else entry.size,
        "etag": _entry_digest(entry, kind="file"),
        "cached": True,
    }


def _resize_image_to_jpeg(source: Path, destination: Path) -> None:
    """Write a long-edge-capped JPEG thumbnail using Pillow (atomic)."""
    from PIL import Image

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".tmp.jpg")
    try:
        with Image.open(source) as image:
            if image.mode != "RGB":
                image = image.convert("RGB")
            image.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.Resampling.LANCZOS)
            image.save(tmp, format="JPEG", quality=THUMB_JPEG_QUALITY, optimize=True)
        _atomic_replace(tmp, destination)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _write_placeholder_jpeg(destination: Path, *, color: tuple[int, int, int] = (39, 39, 42)) -> None:
    from PIL import Image

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".tmp.jpg")
    try:
        # Small solid tile — used when a video poster can't be extracted (no ffmpeg).
        Image.new("RGB", (THUMB_MAX_EDGE, THUMB_MAX_EDGE), color=color).save(
            tmp, format="JPEG", quality=70, optimize=True
        )
        _atomic_replace(tmp, destination)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _extract_video_poster(source: Path, destination: Path) -> bool:
    """Best-effort first-frame JPEG via ffmpeg. Returns False if unavailable."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    import subprocess

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".tmp.jpg")
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "0",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale={THUMB_MAX_EDGE}:{THUMB_MAX_EDGE}:force_original_aspect_ratio=decrease",
                "-q:v",
                "3",
                str(tmp),
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if completed.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(destination)
        return True
    except (OSError, subprocess.TimeoutExpired):
        tmp.unlink(missing_ok=True)
        return False


def _thumb_payload(entry: AssetEntry, dest: Path, etag: str) -> dict[str, Any]:
    return {
        "path": dest.as_posix(),
        "name": f"{PurePosixPath(entry.name).stem}.jpg",
        "media_type": "image/jpeg",
        "size": dest.stat().st_size,
        "etag": etag,
        "cached": True,
    }


def _ensure_thumbnail(entry: AssetEntry) -> dict[str, Any]:
    """Return a resized JPEG thumb path, using a durable on-disk cache."""
    if entry.media_type not in {"image", "video"}:
        raise ValueError("Thumbnails are only available for images and videos.")

    dest = _thumb_path(entry)
    etag = _entry_digest(entry, kind="thumb")
    if dest.exists() and dest.stat().st_size > 0:
        return _thumb_payload(entry, dest, etag)

    # Single-flight per cache key so a grid of identical URLs doesn't stampede Modal.
    with _thumb_lock:
        event = _thumb_inflight.get(etag)
        if event is None:
            event = threading.Event()
            _thumb_inflight[etag] = event
            leader = True
        else:
            leader = False

    if not leader:
        event.wait(timeout=180)
        if dest.exists() and dest.stat().st_size > 0:
            return _thumb_payload(entry, dest, etag)
        raise RuntimeError(f"Thumbnail generation failed for {entry.path}")

    try:
        if not (dest.exists() and dest.stat().st_size > 0):
            if entry.media_type == "image":
                original = Path(_materialize(entry)["path"])
                _resize_image_to_jpeg(original, dest)
            else:
                # Videos: only download full file when ffmpeg can make a poster.
                # Without ffmpeg, use a cheap solid poster so the grid never
                # materializes every mp4 just for a card.
                if shutil.which("ffmpeg"):
                    original = Path(_materialize(entry)["path"])
                    if not _extract_video_poster(original, dest):
                        _write_placeholder_jpeg(dest, color=(24, 24, 27))
                else:
                    _write_placeholder_jpeg(dest, color=(24, 24, 27))
            _evict_cache_if_needed()
        return _thumb_payload(entry, dest, etag)
    finally:
        with _thumb_lock:
            _thumb_inflight.pop(etag, None)
        event.set()


def _entry_from_params(params: dict[str, Any]) -> AssetEntry:
    volume = params["volume"]
    path = normalize_volume_path(params["path"], allow_root=False)
    if params.get("kind") and params.get("modified_at") is not None:
        modified = datetime.fromisoformat(str(params["modified_at"]))
        return AssetEntry(
            volume=volume,
            path=path,
            name=params.get("name") or PurePosixPath(path).name,
            kind=params.get("kind") or "file",
            size=int(params.get("size") or 0),
            modified_at=modified,
            media_type=params.get("media_type") or "file",
        )
    return MANAGER._find_entry(volume, path)  # noqa: SLF001


def handle(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    params = request.get("params") or {}
    request_id = request.get("id")
    try:
        if method == "health":
            result: Any = {"status": "ok", "backend": "python-sdk"}
        elif method == "list":
            volume = params["volume"]
            if volume not in ALLOWED_VOLUMES:
                raise ValueError(f"Unsupported volume: {volume}")
            path = normalize_volume_path(params.get("path", ""), allow_root=True)
            refresh = bool(params.get("refresh", False))
            entries = _load_entries(volume, path, refresh=refresh)
            query = str(params.get("search", "")).strip().casefold()
            if query:
                entries = [e for e in entries if query in e.name.casefold()]
            entries = _sort_entries(entries, str(params.get("sort", "modified_desc")))

            page_size = min(
                max(int(params.get("page_size") or DEFAULT_PAGE_SIZE), 1),
                MAX_PAGE_SIZE,
            )
            total = len(entries)
            page_count = max(1, (total + page_size - 1) // page_size)
            page = min(max(int(params.get("page") or 1), 1), page_count)
            start = (page - 1) * page_size
            page_entries = entries[start : start + page_size]
            image_total = sum(1 for e in entries if e.media_type == "image")
            status = f"{VOLUME_LABELS[volume]}: {total}件"
            if image_total:
                status += f" / 画像 {image_total}件"
            status += f"（{page}/{page_count}ページ）"
            result = {
                "volume": volume,
                "path": path,
                "breadcrumb": _breadcrumb(path),
                "page": page,
                "page_size": page_size,
                "page_count": page_count,
                "total": total,
                "image_total": image_total,
                "status": status,
                "entries": [_record(e) for e in page_entries],
            }
        elif method == "materialize":
            entry = _entry_from_params(params)
            if entry.is_directory:
                raise IsADirectoryError("Directories cannot be downloaded.")
            want_thumb = bool(params.get("image_only") or params.get("thumbnail"))
            if want_thumb:
                result = _ensure_thumbnail(entry)
            else:
                result = _materialize(entry)
        elif method == "upload":
            with LOCK:
                paths = MANAGER.upload_assets(
                    params["volume"],
                    params.get("destination", ""),
                    params.get("files") or [],
                    bool(params.get("overwrite", False)),
                )
            _invalidate_list_cache(params["volume"])
            result = {
                "message": "アップロード完了: " + ", ".join(paths),
                "paths": paths,
            }
        elif method == "move":
            with LOCK:
                moved = MANAGER.move_asset(
                    params["source_volume"],
                    params["source_path"],
                    params["destination_volume"],
                    params["destination_path"],
                    bool(params.get("overwrite", False)),
                )
            _invalidate_list_cache(params["source_volume"])
            _invalidate_list_cache(params["destination_volume"])
            result = {
                "message": f"移動完了: {params['destination_volume']}:{moved}",
                "paths": [moved],
            }
        elif method == "mkdir":
            with LOCK:
                created = MANAGER.create_directory(
                    params["volume"],
                    params["path"],
                )
            _invalidate_list_cache(params["volume"])
            result = {
                "message": f"フォルダ作成: {params['volume']}:{created}",
                "path": created,
                "paths": [created],
            }
        elif method == "delete":
            with LOCK:
                MANAGER.delete_asset(
                    params["volume"],
                    params["path"],
                    recursive=bool(params.get("recursive", False)),
                )
            _invalidate_list_cache(params["volume"])
            result = {
                "message": f"削除完了: {params['volume']}:{params['path']}",
                "paths": [params["path"]],
                "failed": [],
            }
        elif method == "delete_many":
            # Handled by handle_delete_many (supports progress emits + parallelism).
            raise RuntimeError("delete_many must be routed via handle_delete_many")
        elif method == "shutdown":
            result = {"status": "bye"}
        else:
            raise ValueError(f"Unknown method: {method}")
        return {"id": request_id, "ok": True, "result": result}
    except Exception as exc:
        return {
            "id": request_id,
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def _delete_one_with_retry(
    volume: str,
    item_path: str,
    recursive: bool,
    *,
    retries: int = DELETE_RETRIES,
) -> tuple[str, str, str | None]:
    """Return (status, path, error). Retries transient Modal failures."""
    import time

    last_error = "unknown"
    for attempt in range(retries):
        try:
            MANAGER.delete_asset(volume, item_path, recursive=recursive)
            return ("ok", item_path, None)
        except FileNotFoundError:
            # Deletion is idempotent: another worker (or an earlier attempt whose
            # response was lost) may already have removed the asset.
            return ("ok", item_path, None)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            # Back off briefly; Modal often recovers after concurrent pressure.
            time.sleep(0.15 * (attempt + 1))
    return ("err", item_path, last_error)


def handle_delete_many(
    request: dict[str, Any],
    *,
    emit: EmitFn | None = None,
) -> dict[str, Any]:
    """Delete many paths with modest concurrency + retries."""
    request_id = request.get("id")
    params = request.get("params") or {}
    try:
        volume = params["volume"]
        items = params.get("items") or []
        if not items:
            raise ValueError("削除対象が空です。")
        if volume not in ALLOWED_VOLUMES:
            raise ValueError(f"Unsupported volume: {volume}")

        # Cap concurrency hard — high values (e.g. 16) fail against Modal.
        workers = int(params.get("workers") or DEFAULT_DELETE_WORKERS)
        workers = max(1, min(8, workers))
        total = len(items)
        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        lock = threading.Lock()
        done_count = 0

        def _emit_progress() -> None:
            if emit is None:
                return
            emit(
                {
                    "id": request_id,
                    "ok": True,
                    "partial": True,
                    "result": {
                        "done": len(deleted),
                        "failed": len(failed),
                        "total": total,
                        "processed": done_count,
                    },
                }
            )

        def _one(item: dict[str, Any]) -> tuple[str, str, str | None]:
            item_path = str(item.get("path") or "")
            recursive = bool(item.get("recursive", False))
            return _delete_one_with_retry(volume, item_path, recursive)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, item) for item in items]
            for future in as_completed(futures):
                status, item_path, error = future.result()
                with lock:
                    if status == "ok":
                        deleted.append(item_path)
                    else:
                        failed.append({"path": item_path, "error": error or "unknown"})
                    done_count += 1
                _emit_progress()

        # One more sequential pass for leftovers (often recovers after burst).
        if failed:
            retry_items = list(failed)
            failed = []
            for item in retry_items:
                item_path = item["path"]
                # Guess recursive from original payload when possible.
                recursive = any(
                    str(raw.get("path") or "") == item_path
                    and bool(raw.get("recursive", False))
                    for raw in items
                )
                status, path_value, error = _delete_one_with_retry(
                    volume, item_path, recursive, retries=2
                )
                with lock:
                    if status == "ok":
                        deleted.append(path_value)
                    else:
                        failed.append({"path": path_value, "error": error or "unknown"})
                _emit_progress()

        _invalidate_list_cache(volume)
        msg = f"削除完了: {len(deleted)}件"
        if failed:
            msg += f" / 失敗 {len(failed)}件"
        return {
            "id": request_id,
            "ok": True,
            "result": {
                "message": msg,
                "paths": deleted,
                "failed": failed,
                "done": len(deleted),
                "failed_count": len(failed),
                "total": total,
                "workers": workers,
            },
        }
    except Exception as exc:
        return {
            "id": request_id,
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def _write_response(response: dict[str, Any]) -> None:
    payload = json.dumps(response, ensure_ascii=False) + "\n"
    with STDOUT_LOCK:
        sys.stdout.write(payload)
        sys.stdout.flush()


def _is_high_priority(request: dict[str, Any]) -> bool:
    """Sidebar preview / user mutations outrank bulk thumbnail generation."""
    method = request.get("method")
    params = request.get("params") or {}
    if method == "materialize":
        # Full content (preview/download) is high; thumbnails are low.
        return not bool(params.get("image_only") or params.get("thumbnail"))
    if method in {
        "list",
        "upload",
        "move",
        "mkdir",
        "delete",
        "delete_many",
        "health",
        "shutdown",
    }:
        return True
    return False


def _process_request(request: dict[str, Any]) -> None:
    if request.get("method") == "delete_many":
        response = handle_delete_many(request, emit=_write_response)
    else:
        response = handle(request)
    try:
        _write_response(response)
    except BrokenPipeError:
        raise


def _wait_for_queues(
    high_q: Any,
    low_q: Any,
    *,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> None:
    """Wait a bounded time for queued and in-progress work to finish."""
    import time

    deadline = _now() + timeout
    while _now() < deadline and (
        high_q.unfinished_tasks or low_q.unfinished_tasks
    ):
        time.sleep(poll_interval)


def main() -> None:
    """Serve RPC with a high-priority lane so previews aren't stuck behind thumbs.

    Node may flood stdin with thumbnail materialize calls while the user clicks
    an item for the right-hand preview. High-priority work (full materialize,
    list, mutations) is handled by dedicated workers and can also be stolen by
    idle low-priority workers.
    """
    import queue

    high_q: queue.Queue[dict[str, Any] | None] = queue.Queue()
    low_q: queue.Queue[dict[str, Any] | None] = queue.Queue()
    stop = threading.Event()

    high_workers = max(1, min(4, int(os.getenv("COMFY_ASSET_HIGH_WORKERS", "2"))))
    low_workers = max(1, min(8, int(os.getenv("COMFY_ASSET_LOW_WORKERS", "4"))))

    def high_worker() -> None:
        while not stop.is_set():
            request = high_q.get()
            if request is None:
                high_q.task_done()
                return
            try:
                _process_request(request)
                if request.get("method") == "shutdown":
                    stop.set()
            except BrokenPipeError:
                stop.set()
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
            finally:
                high_q.task_done()

    def low_worker() -> None:
        while not stop.is_set():
            request: dict[str, Any] | None
            # Prefer any pending high-priority work so previews jump the thumb queue.
            try:
                request = high_q.get_nowait()
            except queue.Empty:
                try:
                    request = low_q.get(timeout=0.15)
                except queue.Empty:
                    continue
                if request is None:
                    low_q.task_done()
                    return
                try:
                    _process_request(request)
                    if request.get("method") == "shutdown":
                        stop.set()
                except BrokenPipeError:
                    stop.set()
                except Exception:  # noqa: BLE001
                    traceback.print_exc(file=sys.stderr)
                finally:
                    low_q.task_done()
                continue

            # Stolen from high_q
            if request is None:
                high_q.task_done()
                return
            try:
                _process_request(request)
                if request.get("method") == "shutdown":
                    stop.set()
            except BrokenPipeError:
                stop.set()
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
            finally:
                high_q.task_done()

    threads = [
        threading.Thread(target=high_worker, name=f"asset-high-{i}", daemon=True)
        for i in range(high_workers)
    ] + [
        threading.Thread(target=low_worker, name=f"asset-low-{i}", daemon=True)
        for i in range(low_workers)
    ]
    for thread in threads:
        thread.start()

    try:
        for raw in sys.stdin:
            if stop.is_set():
                break
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                _write_response(
                    {
                        "id": None,
                        "ok": False,
                        "error": f"Invalid JSON: {exc}",
                    }
                )
                continue
            if not isinstance(request, dict):
                _write_response(
                    {"id": None, "ok": False, "error": "Request must be a JSON object"}
                )
                continue
            if _is_high_priority(request):
                high_q.put(request)
            else:
                low_q.put(request)
            if request.get("method") == "shutdown":
                # Worker will process shutdown and set stop; keep reading until then.
                break

        # Do not join() indefinitely: after stop, workers may exit without
        # task_done() for leftover queue items (CodeRabbit). Drain with timeout.
        _wait_for_queues(high_q, low_q)
    finally:
        stop.set()
        for _ in threads:
            try:
                high_q.put_nowait(None)
            except Exception:  # noqa: BLE001
                pass
            try:
                low_q.put_nowait(None)
            except Exception:  # noqa: BLE001
                pass
        for thread in threads:
            thread.join(timeout=2.0)
        # Drop unfinished queue accounting so the process can exit cleanly.
        for q in (high_q, low_q):
            while True:
                try:
                    q.get_nowait()
                    q.task_done()
                except Exception:  # noqa: BLE001
                    break
        shutil.rmtree(WORKSPACE, ignore_errors=True)


if __name__ == "__main__":
    main()
