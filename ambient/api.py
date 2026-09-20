import json
from pathlib import Path
import tempfile
import os

from .contracts import DEFAULT_BACKENDS, FPS, FRAMES, RESOLUTIONS, identifier, validate_request
from .service import Conflict
from .storage import AmbientStorage

JOB_BODY_LIMIT = 128 * 1024


def create_api(service, modes, inputs, outputs, *, library=None):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from starlette.background import BackgroundTask
    from starlette.concurrency import run_in_threadpool

    storage = AmbientStorage(inputs, outputs)
    app = FastAPI(title="Ambient video jobs")

    @app.get("/workflows")
    async def workflows():
        import aiohttp
        from .urls import validate_endpoint, redirect_guard
        base = validate_endpoint(os.environ.get("AMBIENT_COMFYUI_URL", ""))
        headers = {"Modal-Key": os.environ.get("MODAL_PROXY_KEY", ""),
                   "Modal-Secret": os.environ.get("MODAL_PROXY_SECRET", "")}
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30),
                                         trace_configs=[redirect_guard()]) as client:
            async with client.get(base + "/ambient/workflows") as response:
                return JSONResponse(await response.json(), status_code=response.status)

    @app.get("/library")
    def list_library():
        if library is None:
            return {"clips": [], "count": 0, "bytes": 0}
        return library.list()

    @app.post("/library", status_code=201)
    async def import_library(request: Request):
        if library is None:
            return JSONResponse({"error": "Library unavailable"}, status_code=503)
        # Stream the raw file to disk before probing/transcoding; do not buffer a
        # complete multipart video in API memory.
        with tempfile.TemporaryDirectory(prefix="ambient-upload-") as directory:
            source = Path(directory) / "upload"
            size = 0
            with source.open("wb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 128 * 1024 * 1024:
                        return JSONResponse({"error": "Video exceeds 128 MiB"}, status_code=413)
                    handle.write(chunk)
            if not size:
                raise ValueError("Expected a video")
            name = request.query_params.get("name", "Imported video")
            try:
                return await run_in_threadpool(library.import_video, source, name)
            except RuntimeError as error:
                return JSONResponse({"error": str(error)[:500]}, status_code=400)

    @app.delete("/library/{clip_id}")
    def delete_library(clip_id: str):
        if library is None:
            raise KeyError(clip_id)
        library.delete(identifier(clip_id))
        return {"deleted": clip_id}

    @app.get("/library/{clip_id}/video")
    def library_video(clip_id: str):
        if library is None:
            raise KeyError(clip_id)
        identifier(clip_id)
        directory = tempfile.TemporaryDirectory(prefix="ambient-library-download-")
        target = Path(directory.name) / "clip.mp4"
        try:
            library.download(clip_id, target)
        except Exception:
            directory.cleanup()
            raise
        return FileResponse(target, media_type="video/mp4", background=BackgroundTask(directory.cleanup),
                            headers={"Cache-Control": "private, no-store"})

    @app.exception_handler(ValueError)
    async def bad_request(_request, error):
        return JSONResponse(
            {"error": str(error)}, status_code=409 if isinstance(error, Conflict) else 400
        )

    @app.exception_handler(KeyError)
    async def not_found(_request, _error):
        return JSONResponse({"error": "Asset or job not found"}, status_code=404)

    @app.get("/capabilities")
    def capabilities():
        return {"modes": modes(), "resolutions": RESOLUTIONS, "frames": FRAMES, "fps": FPS}

    def submit_job(data):
        existing = service.existing(data)
        if existing is not None:
            return existing
        mode = modes()[data["mode"]]
        # Legacy capability providers describe only the legacy default route.
        route = mode.get("backends", {}).get(data["backend"])
        if route is None and data["backend"] == DEFAULT_BACKENDS[data["mode"]]:
            route = mode
        if route is None or not route["ready"]:
            reason = route.get("reason") if route else "Generation backend is not prepared"
            return JSONResponse({"error": reason}, status_code=503)
        if data.get("parentClipId"):
            parent = service.get(data["parentClipId"])
            if parent["status"] != "completed":
                raise ValueError("Parent clip must be completed")
        return service.submit(data)

    @app.post("/jobs", status_code=202)
    async def submit(request: Request):
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > JOB_BODY_LIMIT:
                return JSONResponse({"error": "Job request exceeds 128 KiB"}, status_code=413)
            chunks.append(chunk)
        data = validate_request(json.loads(b"".join(chunks)))
        # Include readiness and parent lookups: these also access the Modal Dict.
        return await run_in_threadpool(submit_job, data)

    @app.get("/jobs/{job_id}")
    def status(job_id: str):
        return service.get(identifier(job_id))

    @app.delete("/jobs/{job_id}")
    def cancel(job_id: str):
        return service.cancel(identifier(job_id))

    @app.post("/images", status_code=201)
    async def upload(request: Request):
        # Bound the entire request before multipart parsing, not just the decoded image.
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 12 * 1024 * 1024:
                return JSONResponse({"error": "Image upload exceeds 12 MiB"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        form = await request.form()
        uploaded = form.get("image")
        if not uploaded or not hasattr(uploaded, "read"):
            await form.close()
            raise ValueError("Expected image upload")
        try:
            data = await uploaded.read()
        finally:
            await form.close()
        asset_id = await run_in_threadpool(storage.save_image, data)
        return {"id": asset_id}

    @app.get("/clips/{clip_id}")
    def clip(clip_id: str):
        job = service.get(identifier(clip_id))
        if job["status"] != "completed":
            raise KeyError(clip_id)
        # Materialize via the Volume SDK: downloads do not wake the GPU, and no mounted
        # volume.reload races with an active FileResponse. FileResponse supports ranges.
        directory = tempfile.TemporaryDirectory(prefix="ambient-download-")
        target = Path(directory.name) / "clip.mp4"
        try:
            storage.download_clip(clip_id, target)
        except Exception:
            directory.cleanup()
            raise
        return FileResponse(
            target,
            media_type="video/mp4",
            background=BackgroundTask(directory.cleanup),
            headers={"Cache-Control": "private, no-store"},
        )

    return app
