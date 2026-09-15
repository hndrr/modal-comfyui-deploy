from __future__ import annotations

import time
from .contracts import fingerprint, public_job, validate_request


class Conflict(ValueError):
    pass


class JobService:
    """Injectable store/dispatcher. Modal Dict.put(skip_if_exists) is the atomic claim."""

    def __init__(self, store, spawn, now=time.time, reconcile=None, reference=None):
        self.store, self.spawn, self.now = store, spawn, now
        self.reconcile = reconcile
        self.reference = reference

    def existing(self, raw):
        """Check retries before readiness or parent checks can reject an accepted job."""
        request = validate_request(raw)
        existing = self.store.get(request["requestId"])
        if existing is None:
            return None
        # Normalize legacy requests too: their stored hash predates backend selection.
        if fingerprint(validate_request(existing["request"])) != fingerprint(request):
            raise Conflict("requestId already belongs to a different request")
        return self.get(request["requestId"])

    def submit(self, raw):
        request = validate_request(raw)
        existing = self.existing(request)
        if existing is not None:
            return existing
        job_id = request["requestId"]
        job = {
            "id": job_id,
            "status": "queued",
            "stage": "Queued",
            "request": request,
            "fingerprint": fingerprint(request),
            "createdAt": self.now(),
        }
        if self.reference:
            job["references"] = self.reference(request)
        if not self.store.put(job_id, job, skip_if_exists=True):
            return self.existing(request)
        try:
            call = self.spawn(job_id)
            # Separate keys prevent dispatch and worker status writes overwriting each other.
            self.store.put("call:" + job_id, str(call))
        except Exception:
            job.update(
                status="failed",
                stage="Dispatch failed",
                error="Job dispatch failed; create a new request to retry.",
            )
            self.store.put(job_id, job)
            raise
        return self.get(job_id)

    def get(self, job_id):
        job = self.store.get(job_id)
        if not job:
            raise KeyError(job_id)
        # A crash between claim and spawn is not permission to generate twice.
        if (
            job["status"] == "queued"
            and self.now() - job["createdAt"] > 300
            and not self.store.get("call:" + job_id)
        ):
            job = {
                **job,
                "status": "failed",
                "stage": "Dispatch failed",
                "error": "Dispatch acknowledgement missing. Inspect Modal before retrying.",
            }
        if (
            job["status"] in ("queued", "running")
            and self.reconcile
            and self.now() - job["createdAt"] > 10
        ):
            call_id = self.store.get("call:" + job_id)
            if call_id:
                reason = self.reconcile(call_id)
                if reason:
                    # Re-read: a completion racing this poll must win over reconciliation.
                    fresh = self.store.get(job_id)
                    if fresh["status"] in ("queued", "running"):
                        job = {
                            **fresh,
                            "status": "failed",
                            "stage": "Worker stopped",
                            "error": reason,
                        }
                    else:
                        job = fresh
        return public_job(job, bool(self.store.get("cancel:" + job_id)))

    def cancel(self, job_id):
        job = self.get(job_id)
        if job["status"] not in ("queued", "running"):
            return job
        self.store.put("cancel:" + job_id, True)
        return self.get(job_id)
