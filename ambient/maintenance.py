"""Retention for Ambient job records and their owned artifacts."""

import time

from .config import RETENTION_SECONDS


def cleanup_jobs(jobs, storage, now=time.time) -> None:
    storage.reload()
    cutoff = now() - RETENTION_SECONDS
    for key, job in list(jobs.items()):
        if ":" in key or not isinstance(job, dict) or job.get("createdAt", cutoff + 1) > cutoff:
            continue
        if job.get("status") not in ("completed", "failed") and not jobs.get("cancel:" + key):
            continue
        storage.remove_clip(key)
        for prefix in ("", "call:", "cancel:", "fast-call:"):
            jobs.pop(prefix + key, None)
    storage.expire_temporary_files(cutoff)
    storage.commit()
