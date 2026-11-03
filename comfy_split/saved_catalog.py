"""Read saved libraries from named Volume roots, without importing ComfyUI."""

import os
import heapq
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from comfy_split.media_server import parts, read_json, catalog_index, saved_job
from comfy_split.job_progress import snapshot

TERMINAL = {"completed", "failed", "cancelled"}
MAX_ENTRIES = 10000


@contextmanager
def directory(root, relative=""):
    fd = os.dup(root) if isinstance(root, int) else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in parts(relative) if relative else ():
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def files(fd, prefix="", *, budget=None, depth=0):
    budget = [MAX_ENTRIES] if budget is None else budget
    if depth > 32:
        raise ValueError("Library directory depth exceeds the read limit")
    rows = []
    with os.scandir(fd) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                raise ValueError("Library exceeds the entry limit; narrow the category")
            try:
                parts(entry.name)
            except ValueError:
                continue
            info = entry.stat(follow_symlinks=False)
            name = prefix + entry.name
            if stat.S_ISDIR(info.st_mode):
                with directory(fd, entry.name) as child:
                    rows.extend(files(child, name + "/", budget=budget, depth=depth + 1))
            elif stat.S_ISREG(info.st_mode):
                rows.append(dict(filename=name, size_bytes=info.st_size, modified_at=info.st_mtime))
    return sorted(rows, key=lambda row: row["filename"])


def paging(q, maximum=200):
    offset, limit = int(q.get("offset", "0")), int(q.get("limit", "100"))
    if offset < 0 or not 1 <= limit <= maximum:
        raise ValueError("Invalid page")
    return offset, limit


def page(rows, offset, limit):
    return dict(total=len(rows), next_offset=offset + limit if offset + limit < len(rows) else None)


class SavedCatalog:
    def __init__(self, data, models):
        self.data, self.models = Path(data), Path(models) if models is not None else None

    def read(self, path, q):
        operation = {"/catalog/models": self.model_list, "/catalog/workflows": self.workflow,
                     "/catalog/history": self.history, "/catalog/job-workflow": self.job_workflow}.get(path)
        if operation is None:
            raise FileNotFoundError("Unknown catalog operation")
        return operation(q)

    def model_list(self, q):
        if set(q) - {"category", "query", "offset", "limit"} or self.models is None:
            raise ValueError("Invalid model inventory request")
        category, query = q.get("category"), q.get("query", "").casefold()
        offset, limit = paging(q)
        if len(query) > 160 or (category is not None and not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", category)):
            raise ValueError("Invalid model category or search")
        with directory(self.models, category or "") as fd:
            if category:
                rows = [r for r in files(fd) if query in r["filename"].casefold()]
                selected = rows[offset:offset + limit]
                return dict(source="storage", recognition="not_checked", category=category,
                            models=[r["filename"] for r in selected], entries=selected,
                            **page(rows, offset, limit))
            with os.scandir(fd) as entries:
                rows = []
                for count, entry in enumerate(entries):
                    if count >= MAX_ENTRIES:
                        raise ValueError("Too many model categories")
                    if (re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", entry.name)
                            and entry.is_dir(follow_symlinks=False) and query in entry.name.casefold()):
                        rows.append(entry.name)
            rows.sort()
            return dict(source="storage", recognition="not_checked", category=None,
                        categories=rows[offset:offset + limit], **page(rows, offset, limit))

    def workflow(self, q):
        action, name, fmt = q.get("action"), q.get("filename", ""), q.get("format", "api")
        if action == "list" and set(q) == {"action"}:
            try:
                with directory(self.data, "user/default/workflows") as fd:
                    rows = [r for r in files(fd) if r["filename"].lower().endswith(".json")]
            except FileNotFoundError:
                rows = []
            return dict(source="storage", workflows=[r["filename"] for r in rows], entries=rows, total=len(rows))
        if action != "get" or set(q) - {"action", "filename", "format"} or fmt not in {"ui", "api"}:
            raise ValueError("Unsupported saved workflow operation")
        parts(name)
        if not name.lower().endswith(".json") or len(name) > 512:
            raise ValueError("Expected a library JSON name")
        raw = read_json(self.data, "user/default/workflows/" + name)
        if not isinstance(raw, dict):
            raise ValueError("Saved workflow is not an object")
        if fmt == "api" and isinstance(raw.get("nodes"), list):
            # Do not guess widget inputs from stale/missing node definitions.
            return dict(source="storage", conversion_required=True)
        return dict(source="storage", conversion_required=False, workflow=raw)

    def job(self, prompt_id):
        job = saved_job(self.data, prompt_id)
        if not job:
            raise FileNotFoundError("Unknown or expired job")
        return job

    def job_workflow(self, q):
        if set(q) != {"prompt_id"}:
            raise ValueError("Expected prompt_id")
        job = self.job(q["prompt_id"])
        graph = job.get("body", {}).get("prompt")
        if not isinstance(graph, dict):
            raise FileNotFoundError("Submitted graph is no longer retained")
        return dict(prompt_id=q["prompt_id"], status=job["status"], workflow=graph,
                    source="storage", live=False)

    def history(self, q):
        action = q.get("action", "list")
        if set(q) - {"action", "prompt_id", "limit"} or action not in {"list", "diagnose"}:
            raise ValueError("Invalid saved history operation")
        limit = int(q.get("limit", "25"))
        if not 1 <= limit <= 100:
            raise ValueError("Invalid history limit")
        if q.get("prompt_id"):
            jobs = [self.job(q["prompt_id"])]
        else:
            def finished():
                for row in catalog_index(self.data):
                    if row["status"] not in TERMINAL:
                        if row["status"] == "queued":
                            continue
                        # A running controller snapshot may already have a
                        # durable GPU result. Only inspect those active records.
                        job = self.job(row["id"])
                        if job["status"] not in TERMINAL:
                            continue
                    yield row
            recent = heapq.nlargest(limit, finished(), key=lambda row: row["created_at"])
            jobs = [self.job(row["id"]) for row in recent]
        if action == "diagnose" and jobs:
            jobs = [next((j for j in jobs if j["status"] == "failed"), jobs[0])]
        rows = []
        for job in jobs:
            # For an active job this is a saved snapshot, not live progress or
            # an estimate of the time it has continued running since the commit.
            info = snapshot(job, now=job.get("finished_at", job.get("created_at", 0)))
            if job["status"] not in TERMINAL:
                info.update(progress=None, elapsed_seconds=None)
            info.update(outputs=(job.get("history") or {}).get("outputs", {}))
            rows.append(info)
        return dict(source="storage", live=False, items=rows,
                    note="Saved records only. Use get_job_progress for live state; no generation was submitted.")
