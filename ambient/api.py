
import io
from pathlib import Path
import tempfile
from uuid import uuid4

from .contracts import FPS, FRAMES, RESOLUTIONS, identifier, validate_request
from .service import Conflict


def create_api(service, modes, inputs, outputs):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from starlette.background import BackgroundTask
    from PIL import Image, ImageOps
    app = FastAPI(title='Ambient video jobs')

    @app.exception_handler(ValueError)
    async def bad_request(_request, error):
        return JSONResponse({'error': str(error)}, status_code=409 if isinstance(error, Conflict) else 400)

    @app.exception_handler(KeyError)
    async def not_found(_request, _error):
        return JSONResponse({'error': 'Asset or job not found'}, status_code=404)

    @app.get('/capabilities')
    def capabilities():
        return {'modes': modes(), 'resolutions': RESOLUTIONS, 'frames': FRAMES, 'fps': FPS}

    @app.post('/jobs', status_code=202)
    async def submit(request: Request):
        data = validate_request(await request.json())
        mode = modes()[data['mode']]
        if not mode['ready']:
            return JSONResponse({'error': mode['reason']}, status_code=503)
        if data.get('parentClipId'):
            parent = service.get(data['parentClipId'])
            if parent['status'] != 'completed':
                raise ValueError('Parent clip must be completed')
        # Store operations / Modal dispatch run outside the ASGI event loop.
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(service.submit, data)

    @app.get('/jobs/{job_id}')
    def status(job_id: str):
        return service.get(identifier(job_id))

    @app.delete('/jobs/{job_id}')
    def cancel(job_id: str):
        return service.cancel(identifier(job_id))

    @app.post('/images', status_code=201)
    async def upload(request: Request):
        # Bound the entire request before multipart parsing, not just the decoded image.
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 12*1024*1024:
                return JSONResponse({'error': 'Image upload exceeds 12 MiB'}, status_code=413)
            chunks.append(chunk)
        request._body = b''.join(chunks)
        form = await request.form()
        uploaded = form.get('image')
        if not uploaded or not hasattr(uploaded, 'read'):
            await form.close()
            raise ValueError('Expected image upload')
        try:
            data = await uploaded.read()
        finally:
            await form.close()
        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.width*source.height > 24_000_000:
                    raise ValueError('Image exceeds 24 megapixels')
                image = ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((1920, 1920))
                output = io.BytesIO(); image.save(output, format='PNG')
        except (OSError, Image.DecompressionBombError) as error:
            raise ValueError('Invalid image') from error
        asset_id = str(uuid4())
        from starlette.concurrency import run_in_threadpool
        def save():
            with inputs.batch_upload() as batch:
                batch.put_file(io.BytesIO(output.getvalue()), f'ambient/images/{asset_id}.png')
        await run_in_threadpool(save)
        return {'id': asset_id}

    @app.get('/clips/{clip_id}')
    def clip(clip_id: str):
        job = service.get(identifier(clip_id))
        if job['status'] != 'completed':
            raise KeyError(clip_id)
        # Materialize via the Volume SDK: downloads do not wake the GPU, and no mounted
        # volume.reload races with an active FileResponse. FileResponse supports ranges.
        directory = tempfile.TemporaryDirectory(prefix='ambient-download-')
        target = Path(directory.name)/'clip.mp4'
        try:
            with target.open('wb') as handle:
                outputs.read_file_into_fileobj(f'ambient/clips/{clip_id}.mp4', handle)
        except Exception:
            directory.cleanup()
            raise
        return FileResponse(target, media_type='video/mp4', background=BackgroundTask(directory.cleanup), headers={'Cache-Control': 'private, no-store'})
    return app
