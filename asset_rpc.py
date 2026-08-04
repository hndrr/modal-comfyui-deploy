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
# Modal Volume delete is rate-limited / flaky under high concurrency.
# Keep this low (2–3). Override with ASSET_DELETE_WORKERS if needed.
DEFAULT_DELETE_WORKERS = max(1, min(8, int(os.getenv("ASSET_DELETE_WORKERS", "4"))))
DELETE_RETRIES = max(1, int(os.getenv("ASSET_DELETE_RETRIES", "3")))

MANAGER = AssetManager()
LOCK = threading.Lock()
STDOUT_LOCK = threading.Lock()
WORKSPACE = Path(tempfile.mkdtemp(prefix="comfy-asset-rpc-"))
CACHE_DIR = WORKSPACE / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

EmitFn = Callable[[dict[str, Any]], None]

# volume:path -> (monotonic expires, entries)
_list_cache: dict[str, tuple[float, list[AssetEntry]]] = {}


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


def _cached_path(entry: AssetEntry) -> Path:
    digest = hashlib.sha256(
        f"{entry.volume}:{entry.path}:{entry.modified_at.isoformat()}".encode()
    ).hexdigest()
    return CACHE_DIR / f"{digest}{PurePosixPath(entry.path).suffix}"


def _materialize(entry: AssetEntry) -> dict[str, Any]:
    destination = _cached_path(entry)
    if not destination.exists():
        MANAGER.download_listed_asset(entry, destination)
    media_type = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
    return {
        "path": destination.as_posix(),
        "name": entry.name,
        "media_type": media_type,
        "size": entry.size,
    }


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
            if params.get("image_only") and entry.media_type != "image":
                raise ValueError("Thumbnails are only available for images.")
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


def main() -> None:
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                response: dict[str, Any] = {
                    "id": None,
                    "ok": False,
                    "error": f"Invalid JSON: {exc}",
                }
            else:
                if isinstance(request, dict) and request.get("method") == "delete_many":
                    response = handle_delete_many(request, emit=_write_response)
                else:
                    response = handle(request)
            try:
                _write_response(response)
            except BrokenPipeError:
                break
            if isinstance(request, dict) and request.get("method") == "shutdown":
                break
    finally:
        shutil.rmtree(WORKSPACE, ignore_errors=True)


if __name__ == "__main__":
    main()
