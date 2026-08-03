"""Long-lived JSONL RPC for Modal Volume ops (used by the Hono server).

Keeps Modal SDK warm. List pagination stays in Python so Node never
receives 20k-entry payloads on every page flip.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import sys
import tempfile
import threading
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

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
}
DEFAULT_PAGE_SIZE = 48
MAX_PAGE_SIZE = 200
LIST_CACHE_TTL_SEC = 60.0

MANAGER = AssetManager()
LOCK = threading.Lock()
WORKSPACE = Path(tempfile.mkdtemp(prefix="comfy-asset-rpc-"))
CACHE_DIR = WORKSPACE / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

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
        sort_mode = "name_asc"
    reverse = sort_mode.endswith("_desc")

    def sort_key(item: AssetEntry):
        if sort_mode.startswith("modified"):
            return item.modified_at
        if sort_mode.startswith("size"):
            return item.size
        return item.name.casefold()

    sorted_entries = sorted(entries, key=sort_key, reverse=reverse)
    return sorted(sorted_entries, key=lambda item: not item.is_directory)


def _load_entries(volume: str, path: str) -> list[AssetEntry]:
    key = f"{volume}:{path}"
    cached = _list_cache.get(key)
    if cached and cached[0] > _now():
        return cached[1]
    entries = MANAGER.list_assets(volume, path)
    _list_cache[key] = (_now() + LIST_CACHE_TTL_SEC, entries)
    return entries


def _invalidate_list_cache(volume: str | None = None) -> None:
    if volume is None:
        _list_cache.clear()
        return
    prefix = f"{volume}:"
    for key in list(_list_cache):
        if key.startswith(prefix):
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
            entries = list(_load_entries(volume, path))
            query = str(params.get("search", "")).strip().casefold()
            if query:
                entries = [e for e in entries if query in e.name.casefold()]
            entries = _sort_entries(entries, str(params.get("sort", "name_asc")))

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
            }
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


def main() -> None:
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                response = {"id": None, "ok": False, "error": f"Invalid JSON: {exc}"}
            else:
                response = handle(request)
            try:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                break
            if isinstance(request, dict) and request.get("method") == "shutdown":
                break
    finally:
        shutil.rmtree(WORKSPACE, ignore_errors=True)


if __name__ == "__main__":
    main()
