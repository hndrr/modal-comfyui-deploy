"""Retention for Ambient job records and their owned artifacts."""

import time

from .config import RETENTION_SECONDS
from .contracts import TERMINAL
from .job_state import read_job


def cleanup_jobs(jobs, storage, now=time.time) -> None:
    storage.reload()
    cutoff = now() - RETENTION_SECONDS
    for key, job in list(jobs.items()):
        if ":" in key or not isinstance(job, dict) or job.get("createdAt", cutoff + 1) > cutoff:
            continue
        if read_job(jobs, key)["status"] not in TERMINAL:
            continue
        storage.remove_clip(key)
        for prefix in ("", "call:", "cancel:", "fast-call:", "terminal:"):
            jobs.pop(prefix + key, None)
    storage.expire_temporary_files(cutoff)
    storage.commit()
