from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from uuid import uuid4

from .contracts import FRAMES, RESOLUTIONS, prompt_text

MODEL_FILES = {
    'unet': 'minimax_h3_fl2va_pruned_int8_convrot.safetensors',
    'clip': 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
    'video_vae': 'minimax_h3_video_vae_fp16.safetensors',
    'audio_vae': 'minimax_h3_audio_vae_fp32.safetensors',
    'lora': 'minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors',
}


def workflow(request: dict, image_name: str | None = None) -> dict:
    width, height = RESOLUTIONS[request['resolution']]
    def node(kind, **inputs):
        return {'class_type': kind, 'inputs': inputs}
    graph = {
        '1': node('UNETLoader', unet_name=MODEL_FILES['unet'], weight_dtype='default'),
        '2': node('LoraLoaderModelOnly', model=['1', 0], lora_name=MODEL_FILES['lora'], strength_model=1.0),
        '3': node('CLIPLoader', clip_name=MODEL_FILES['clip'], type='minimax', device='default'),
        '4': node('VAELoader', vae_name=MODEL_FILES['video_vae']),
        '5': node('VAELoader', vae_name=MODEL_FILES['audio_vae']),
        '6': node('MiniMaxH3ImageToVideo', clip=['3', 0], vae=['4', 0], prompt=prompt_text(request), width=width, height=height, length=FRAMES),
        '7': node('BasicGuider', model=['2', 0], conditioning=['6', 0]),
        '8': node('RandomNoise', noise_seed=request['seed']),
        '9': node('KSamplerSelect', sampler_name='res_multistep'),
        '10': node('BasicScheduler', model=['2', 0], scheduler='simple', steps=8, denoise=1.0),
        '11': node('SamplerCustomAdvanced', noise=['8', 0], guider=['7', 0], sampler=['9', 0], sigmas=['10', 0], latent_image=['6', 1]),
        '12': node('VAEDecode', samples=['11', 0], vae=['4', 0]),
        '13': node('VAEDecodeAudio', samples=['11', 0], vae=['5', 0]),
        '14': node('CreateVideo', images=['12', 0], audio=['13', 0], fps=24.0),
        '15': node('SaveVideo', video=['14', 0], filename_prefix=f"ambient/raw/{request['requestId']}", format='mp4', codec='h264'),
    }
    if image_name:
        graph['16'] = node('LoadImage', image=image_name)
        graph['6']['inputs']['first_frame'] = ['16', 0]
    return graph


def output_file(history: dict) -> dict:
    for output in history.get('outputs', {}).values():
        for key in ('images', 'gifs', 'videos'):
            for item in output.get(key, []):
                if isinstance(item, dict) and str(item.get('filename', '')).lower().endswith(('.mp4', '.webm')):
                    return {k: item[k] for k in ('filename', 'subfolder', 'type') if k in item}
    raise RuntimeError('ComfyUI finished without a video output')


async def generate(base: str, headers: dict, request: dict, image: Path | None, destination: Path, cancelled, progress, timeout: int = 1800) -> None:
    import aiohttp
    client_id = str(uuid4())
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as session:
        async def call(method, path, **kwargs):
            async with session.request(method, base.rstrip('/')+path, **kwargs) as response:
                if response.status >= 400:
                    raise RuntimeError(f'ComfyUI {path}: HTTP {response.status}: {(await response.text())[:600]}')
                return await response.json()
        # This control-only backend connection keeps the existing web_server input active.
        # Never forward preview bytes, and disable compression for Modal's proxy.
        async with session.ws_connect(base.rstrip('/')+'/ws?clientId='+client_id, compress=0, heartbeat=15) as socket:
            async def drain():
                async for message in socket:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        event = json.loads(message.data)
                        if event.get('type') == 'progress':
                            progress('Sampling')
            drain_task = asyncio.create_task(drain())
            try:
                image_name = None
                if image:
                    form = aiohttp.FormData()
                    form.add_field('image', image.read_bytes(), filename=f"ambient-{request['requestId']}.png", content_type='image/png')
                    form.add_field('type', 'input')
                    form.add_field('subfolder', 'ambient/uploads')
                    uploaded = await call('POST', '/upload/image', data=form)
                    image_name = '/'.join(filter(None, [uploaded.get('subfolder'), uploaded['name']]))
                submitted = await call('POST', '/prompt', json={'prompt': workflow(request, image_name), 'client_id': client_id})
                if submitted.get('node_errors'):
                    raise RuntimeError(f"H3 workflow validation failed: {submitted['node_errors']}")
                prompt_id = submitted['prompt_id']
                progress('ComfyUI queued')
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    history = (await call('GET', f'/history/{prompt_id}')).get(prompt_id)
                    if history:
                        if cancelled():
                            return
                        if history.get('status', {}).get('status_str') == 'error':
                            raise RuntimeError(f"ComfyUI generation failed: {history.get('status', {}).get('messages', [])[-1:]}")
                        if not history.get('status', {}).get('completed', False):
                            await asyncio.sleep(1)
                            continue
                        artifact = output_file(history)
                        progress('Downloading generated video')
                        async with session.get(base.rstrip('/')+'/view', params=artifact) as response:
                            response.raise_for_status()
                            with destination.open('wb') as handle:
                                async for chunk in response.content.iter_chunked(1024*1024): handle.write(chunk)
                        return
                    if cancelled():
                        queue = await call('GET', '/queue')
                        # Deleting this ID from pending is safe even if it just began running.
                        await call('POST', '/queue', json={'delete': [prompt_id]})
                        running = any(row[1] == prompt_id for row in queue.get('queue_running', []))
                        if not running:
                            return
                        # Logical cancellation: drain only our running job. Never /interrupt.
                    if socket.closed:
                        raise RuntimeError('ComfyUI control connection closed; job result is uncertain. Inspect ComfyUI history before retrying.')
                    await asyncio.sleep(1)
                raise TimeoutError('ComfyUI generation timed out; upstream result may still be running')
            finally:
                drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)
