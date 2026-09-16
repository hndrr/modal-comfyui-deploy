"""Keep a job's terminal result separate from mutable worker progress."""

from .contracts import TERMINAL


def read_job(jobs, job_id: str) -> dict | None:
    job = jobs.get(job_id)
    if job is None:
        return None
    terminal = jobs.get("terminal:" + job_id)
    if terminal is not None:
        return terminal
    # Keep records written before terminal claims readable without a migration.
    if jobs.get("cancel:" + job_id):
        return {**job, "status": "cancelled", "stage": "Cancelled"}
    return job


def finish_job(jobs, job_id: str, **result) -> dict:
    """Atomically choose the first terminal result, including its clip metadata."""
    job = read_job(jobs, job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] not in TERMINAL:
        job = {**job, **result}
    # One conditional write chooses the winner across API/worker containers.
    # Never publish completed in the progress record before this claim succeeds.
    jobs.put("terminal:" + job_id, job, skip_if_exists=True)
    return jobs.get("terminal:" + job_id)
