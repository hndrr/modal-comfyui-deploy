"""Run a generation job using injected storage and generation backends."""

from collections.abc import Callable, Mapping
from pathlib import Path
import tempfile

from .storage import AmbientStorage
from .contracts import stored_backend, validate_request


Generator = Callable[[dict, Path | None, Path, Callable[[], bool], Callable[[str], None]], None]


def run_job(
    job_id: str, jobs, storage: AmbientStorage, generators: Mapping[tuple[str, str], Generator]
) -> None:
    """Generators write a source video; storage encodes and commits both artifacts."""
    job = jobs[job_id]
    request = {**job["request"], "backend": stored_backend(job["request"])}

    def cancelled():
        return bool(jobs.get("cancel:" + job_id))

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
            job.update(status="completed", stage="Complete", clip=clip)
            jobs.put(job_id, job)
    except Exception as error:
        job.update(status="failed", stage="Failed", error=str(error)[:1800])
        jobs.put(job_id, job)
        print(f"Ambient job {job_id} failed: {type(error).__name__}: {error}")
