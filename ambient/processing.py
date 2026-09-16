"""Run a generation job using injected storage and generation backends."""

from collections.abc import Callable, Mapping
from pathlib import Path
import tempfile

from .storage import AmbientStorage
from .contracts import stored_backend, validate_request
from .job_state import finish_job, read_job


Generator = Callable[[dict, Path | None, Path, Callable[[], bool], Callable[[str], None]], None]


def run_job(
    job_id: str, jobs, storage: AmbientStorage, generators: Mapping[tuple[str, str], Generator]
) -> None:
    """Generators write a source video; storage encodes and commits both artifacts."""
    job = jobs[job_id]
    request = {**job["request"], "backend": stored_backend(job["request"])}

    def cancelled():
        return read_job(jobs, job_id)["status"] == "cancelled"

    def progress(stage):
        job.update(status="running", stage=stage)
        jobs.put(job_id, job)

    try:
        if cancelled():
            return
        request = validate_request(request)
        progress("Preparing generation")
        with tempfile.TemporaryDirectory(prefix="ambient-job-") as directory:
            root = Path(directory)
            source = root / "generated.mp4"
            image = storage.prepare_anchor(request, root)
            generators[(request["mode"], request["backend"])](
                request, image, source, cancelled, progress
            )
            if cancelled():
                return
            progress("Encoding video and audio")
            clip = storage.publish_clip(source, job_id)
            if cancelled():
                return
            finish_job(jobs, job_id, status="completed", stage="Complete", clip=clip)
    except Exception as error:
        finish_job(jobs, job_id, status="failed", stage="Failed", error=str(error)[:1800])
        print(f"Ambient job {job_id} failed: {type(error).__name__}: {error}")
