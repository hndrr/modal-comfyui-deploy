"""Deploy with ./scripts/modal.sh deploy ambient_app.py (same pinned profile as ComfyUI)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time

import modal
import comfyapp  # Reuse this repo's dotenv resolution, GPU profile and Volume definitions.
from ambient.config import FASTVIDEO_REF, FAST_MODEL, MODEL_ROOT, RETENTION_SECONDS, FAST_MODEL_REVISION
from ambient.contracts import RESOLUTIONS
from ambient.service import JobService

app = modal.App('comfyui-ambient')
BASE = Path(__file__).parent
COMFY_URL = os.environ.get('AMBIENT_COMFYUI_URL', '').rstrip('/')
MODEL_REVISION = os.environ.get('AMBIENT_FASTH3_MODEL_REVISION', '')
for value, label in ((MODEL_REVISION, 'AMBIENT_FASTH3_MODEL_REVISION'),):
    if value and (len(value) != 40 or any(c not in '0123456789abcdef' for c in value)):
        raise ValueError(f'{label} must be a full Hugging Face commit SHA')

cpu_image = (modal.Image.debian_slim(python_version='3.12')
    .apt_install('ffmpeg')
    .pip_install('fastapi==0.115.14', 'starlette==0.46.2', 'python-multipart==0.0.22', 'aiohttp==3.12.15', 'pillow==11.3.0', 'huggingface_hub==0.34.4', 'python-dotenv==1.1.1')
    .add_local_dir(BASE/'ambient', remote_path='/root/ambient'))

# Separate dependencies: FastVideo cannot replace the shared ComfyUI's torch stack.
fast_image = (modal.Image.from_registry('nvidia/cuda:13.0.0-devel-ubuntu24.04', add_python='3.12')
    .apt_install('git', 'ffmpeg', 'build-essential', 'libgl1', 'libglib2.0-0')
    .pip_install('uv', 'python-dotenv==1.1.1')
    .run_commands(f'git clone https://github.com/hao-ai-lab/FastVideo.git /opt/FastVideo && cd /opt/FastVideo && git checkout {FASTVIDEO_REF}',
                  'cd /opt/FastVideo && UV_TORCH_BACKEND=cu130 uv pip install --system --no-sources-package fastvideo-kernel -e ".[fasth3]"')
    .env({'PYTHONPATH': '/opt/FastVideo:/root', 'FASTVIDEO_ATTENTION_BACKEND': 'VIDEO_SPARSE_ATTN_H3'})
    .add_local_dir(BASE/'ambient', remote_path='/root/ambient'))

configuration = modal.Secret.from_dict({
    'AMBIENT_COMFYUI_URL': COMFY_URL,
    'MODAL_PROXY_KEY': os.environ.get('MODAL_PROXY_KEY', ''),
    'MODAL_PROXY_SECRET': os.environ.get('MODAL_PROXY_SECRET', ''),
    'AMBIENT_FASTH3_MODEL_REVISION': MODEL_REVISION,
})


def store():
    return modal.Dict.from_name('comfyui-ambient-jobs', create_if_missing=True)


def is_cancelled(job_id):
    return bool(store().get('cancel:'+job_id))


@app.cls(image=fast_image, gpu=str(comfyapp.GPU_PROFILE['modal_gpu']), memory=196608,
         timeout=comfyapp.FUNCTION_TIMEOUT, scaledown_window=comfyapp.SCALEDOWN_WINDOW,
         min_containers=0, max_containers=1, volumes={'/models': comfyapp.volume}, secrets=[configuration])
class FastH3:
    @modal.enter()
    def load(self):
        import sys
        sys.path.insert(0, '/opt/FastVideo/examples/inference/basic')
        from basic_fasth3 import parse_args, configure_environment, build_generator_config
        from fastvideo import VideoGenerator
        revision = os.environ['AMBIENT_FASTH3_MODEL_REVISION']
        model = Path(MODEL_ROOT)/revision
        if not (model/'modular_model_index.json').exists():
            raise RuntimeError('Prepare the pinned FastH3 model snapshot first')
        self.args = parse_args(['--prompt', 'ambient', '--model-path', str(model), '--num-gpus', '1',
                               '--vsa-kernel', 'triton', '--no-fa4', '--no-parallel-vae',
                               '--no-replicated-dit', '--no-inference-torch-compile', '--no-compile-vae', '--profile', 'strict'])
        configure_environment(self.args)
        self.generator = VideoGenerator.from_config(build_generator_config(self.args))

    @modal.method()
    def generate(self, request):
        from basic_fasth3 import build_request, _actual_output_path
        from ambient.contracts import prompt_text
        if is_cancelled(request['requestId']):
            return None
        self.args.prompt = prompt_text(request)
        self.args.width, self.args.height = RESOLUTIONS[request['resolution']]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'generated.mp4'
            result = self.generator.generate(build_request(self.args, target, request['seed']))
            return _actual_output_path(result, target).read_bytes()

    @modal.exit()
    def unload(self):
        if hasattr(self, 'generator'):
            self.generator.shutdown()


@app.function(image=cpu_image, timeout=comfyapp.FUNCTION_TIMEOUT, max_containers=1,
              volumes={'/inputs': comfyapp.input_volume, '/outputs': comfyapp.output_volume}, secrets=[configuration])
def process_job(job_id: str):
    from PIL import Image, ImageOps
    from ambient import comfy
    from ambient.media import finalize
    jobs = store()
    job = jobs[job_id]
    request = job['request']
    def progress(stage):
        job.update(status='running', stage=stage)
        jobs.put(job_id, job)
    try:
        if is_cancelled(job_id):
            return
        progress('Preparing generation')
        with tempfile.TemporaryDirectory(prefix='ambient-job-') as directory:
            root = Path(directory)
            source = root/'generated.mp4'
            image = None
            image_path = (f"ambient/images/{request['imageId']}.png" if request.get('imageId') else
                          f"ambient/frames/{request['parentClipId']}.png" if request.get('parentClipId') else None)
            if image_path:
                image = root/'anchor.png'
                with image.open('wb') as handle:
                    comfyapp.input_volume.read_file_into_fileobj(image_path, handle)
                # H3's first-frame input stretches; crop to the output aspect before upload.
                with Image.open(image) as incoming:
                    ImageOps.fit(incoming.convert('RGB'), RESOLUTIONS[request['resolution']]).save(root/'fitted.png')
                image = root/'fitted.png'
            if request['mode'] == 'h3':
                headers = {}
                key, secret = os.environ.get('MODAL_PROXY_KEY'), os.environ.get('MODAL_PROXY_SECRET')
                if bool(key) != bool(secret):
                    raise ValueError('ComfyUI proxy auth is incomplete')
                if key:
                    headers = {'Modal-Key': key, 'Modal-Secret': secret}
                asyncio.run(comfy.generate(os.environ['AMBIENT_COMFYUI_URL'], headers, request, image, source,
                                           lambda: is_cancelled(job_id), progress, max(60, comfyapp.FUNCTION_TIMEOUT-180)))
            else:
                progress('FastH3 sampling')
                call = FastH3().generate.spawn(request)
                jobs.put('fast-call:'+job_id, call.object_id)
                result = call.get()
                if result is not None:
                    source.write_bytes(result)
            if is_cancelled(job_id):
                return
            progress('Encoding video and audio')
            target = Path('/outputs/ambient/clips')/f'{job_id}.mp4'
            frame = Path('/inputs/ambient/frames')/f'{job_id}.png'
            clip = finalize(source, target, frame, job_id)
            # Consumers must never see completed before BOTH files are durable.
            comfyapp.input_volume.commit()
            comfyapp.output_volume.commit()
            if is_cancelled(job_id):
                return
            job.update(status='completed', stage='Complete', clip=clip)
            jobs.put(job_id, job)
    except Exception as error:
        job.update(status='failed', stage='Failed', error=str(error)[:1800])
        jobs.put(job_id, job)
        print(f'Ambient job {job_id} failed: {type(error).__name__}: {error}')


@app.function(image=cpu_image, min_containers=0, max_containers=1, secrets=[configuration])
@modal.concurrent(max_inputs=10)
@modal.asgi_app(requires_proxy_auth=True)
def api():
    from ambient.api import create_api
    def modes():
        jobs = store()
        h3_record = jobs.get('prepared:h3') or {}
        fast_record = jobs.get('prepared:fasth3') or {}
        h3 = bool(COMFY_URL) and h3_record.get('url') == COMFY_URL
        fast = bool(MODEL_REVISION) and fast_record.get('revision') == MODEL_REVISION
        return {'h3': {'ready': h3, 'imageInput': True, 'camera': True, 'continuity': True, 'audio': True, 'steps': 8,
                       'reason': None if h3 else 'Set AMBIENT_COMFYUI_URL, prepare H3 models, then run check_h3', 'validation': h3_record},
                'fasth3': {'ready': fast, 'imageInput': False, 'camera': False, 'continuity': False, 'audio': True, 'steps': 4,
                          'reason': None if fast else 'Run prepare_fasth3 with the pinned revision and redeploy', 'validation': fast_record}}
    def reconcile(call_id):
        try:
            modal.FunctionCall.from_id(call_id).get(timeout=0)
            return 'Worker exited before publishing its result; inspect Modal logs.'
        except modal.exception.TimeoutError:
            return None
        except Exception as error:
            return f'Worker terminated: {type(error).__name__}'
    service = JobService(store(), lambda job_id: process_job.spawn(job_id).object_id, reconcile=reconcile)
    return create_api(service, modes, comfyapp.input_volume, comfyapp.output_volume)


@app.function(image=cpu_image, timeout=86400, max_containers=1, volumes={'/models': comfyapp.volume},
              secrets=[modal.Secret.from_name('huggingface-secret')])
def prepare_fasth3(revision: str = FAST_MODEL_REVISION):
    from huggingface_hub import snapshot_download
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('Pass a full Hugging Face commit SHA')
    target = Path(MODEL_ROOT)/revision
    snapshot_download(FAST_MODEL, revision=revision, local_dir=target)
    comfyapp.volume.commit()
    record = {'model': FAST_MODEL, 'revision': revision, 'fastvideo': FASTVIDEO_REF, 'gpuValidated': False}
    store().put('prepared:fasth3', record)
    return record


@app.function(image=cpu_image, schedule=modal.Period(hours=6),
              volumes={'/inputs': comfyapp.input_volume, '/outputs': comfyapp.output_volume})
def cleanup():
    comfyapp.input_volume.reload(); comfyapp.output_volume.reload()
    jobs = store(); cutoff = time.time()-RETENTION_SECONDS
    # Only our namespaces, never the user's other ComfyUI assets or model files.
    for key, job in list(jobs.items()):
        if ':' in key or not isinstance(job, dict) or job.get('createdAt', cutoff+1)>cutoff:
            continue
        if job.get('status') not in ('completed','failed') and not is_cancelled(key):
            continue
        for path in (Path('/outputs/ambient/clips')/f'{key}.mp4', Path('/inputs/ambient/frames')/f'{key}.png'):
            path.unlink(missing_ok=True)
        for prefix in ('', 'call:', 'cancel:', 'fast-call:'):
            jobs.pop(prefix+key, None)
    # Orphan uploaded anchors and raw outputs expire independently of job success.
    for root in (Path('/inputs/ambient/images'), Path('/inputs/ambient/uploads'), Path('/outputs/ambient/raw')):
        if root.exists():
            for path in root.rglob('*'):
                if path.is_file() and path.stat().st_mtime<cutoff:
                    path.unlink()
    comfyapp.input_volume.commit(); comfyapp.output_volume.commit()


@app.function(image=cpu_image, timeout=300, secrets=[configuration])
def check_h3():
    """Read native node/model inventory; does not enqueue a GPU generation."""
    import aiohttp
    from ambient.readiness import validate_object_info
    from ambient.config import COMFYUI_REFERENCE
    async def check():
        headers = {'Modal-Key': os.environ.get('MODAL_PROXY_KEY', ''), 'Modal-Secret': os.environ.get('MODAL_PROXY_SECRET', '')}
        url = os.environ['AMBIENT_COMFYUI_URL'].rstrip('/')
        if not url:
            raise ValueError('Set AMBIENT_COMFYUI_URL before deployment')
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=240)) as client:
            async with client.get(url+'/object_info') as response:
                response.raise_for_status(); info = await response.json()
            validate_object_info(info)
            async with client.get(url+'/system_stats') as response:
                response.raise_for_status(); stats = await response.json()
        record = {'url': url, 'comfyVersion': stats.get('system', {}).get('comfyui_version'),
                  'workflowReference': COMFYUI_REFERENCE, 'checkedAt': time.time(), 'gpuValidated': False}
        store().put('prepared:h3', record)
        return record
    return asyncio.run(check())
