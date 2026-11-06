"""CPU-only ComfyUI smoke check; read-only production volumes, no provider calls.

scripts/modal.sh run -m scripts.verify_split_cpu
Provider execution needs a separately approved workflow.
"""

import asyncio
import json
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import modal

from comfy_split.storage import MOUNTS, VOLUME_NAMES

if modal.is_local():
    from splitapp import image
else:
    image = None

app = modal.App("split-cpu-verification")


@app.function(image=image, cpu=2, memory=8192, timeout=600, max_containers=1,
              scaledown_window=2, retries=0,
              volumes={MOUNTS[k]: modal.Volume.from_name(VOLUME_NAMES[k]).read_only()
                       for k in ("environment", "data", "models")})
async def verify():
    from aiohttp import ClientSession
    from comfy_split.runtime import ComfyProcess

    version = json.loads(Path("/split-data/state/controller.json").read_text())["environment"]
    process = ComfyProcess("cpu_worker", 8188)
    await process.start(version, cpu=True)
    try:
        async with ClientSession() as client:
            async def get(path):
                async with client.get(process.url + path) as r:
                    r.raise_for_status()
                    return await r.json()

            async def run(graph):
                async with client.post(process.url + "/prompt", json={"prompt": graph}) as r:
                    accepted = await r.json()
                    assert r.status == 200, accepted
                async with asyncio.timeout(90):
                    while True:
                        entry = (await get("/history/" + accepted["prompt_id"])).get(accepted["prompt_id"])
                        if entry:
                            assert entry["status"]["status_str"] == "success", entry["status"]
                            return entry["outputs"]
                        await asyncio.sleep(.2)

            stats = await get("/system_stats")
            assert all(d["type"] == "cpu" for d in stats["devices"]), stats
            assert not (await get("/_split/catalog"))["cpu_guard"]
            graph = {
                "1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 3368601}},
                "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "cpu-check"}},
                "3": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}},
            }
            outputs = await run(graph)
            saved = outputs["2"]["images"][0]
            source = Path("/data/output") / saved["subfolder"] / saved["filename"]
            shutil.copyfile(source, "/data/input/cpu-check.png")
            graph["1"] = {"class_type": "LoadImage", "inputs": {"image": "cpu-check.png"}}
            await run(graph)
            with wave.open("/data/input/cpu-check.wav", "wb") as audio:
                audio.setparams((1, 2, 44100, 44100, "NONE", "not compressed"))
                audio.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 44100)))
                                          for i in range(44100)))
            graph = {"1": {"class_type": "LoadAudio", "inputs": {"audio": "cpu-check.wav"}}}
            for index, name in enumerate(("SaveAudio", "SaveAudioMP3", "SaveAudioOpus", "PreviewAudio"), 2):
                graph[str(index)] = {"class_type": name, "inputs": {"audio": ["1", 0]}}
                if name != "PreviewAudio":
                    graph[str(index)]["inputs"]["filename_prefix"] = "cpu-check"
            await run(graph)
            subprocess.run(["ffmpeg", "-v", "error", "-loop", "1", "-i", str(source), "-t", "1",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "/data/input/cpu-check.mp4"], check=True)
            await run({"1": {"class_type": "LoadVideo", "inputs": {"file": "cpu-check.mp4"}},
                       "2": {"class_type": "SaveVideo", "inputs": {"video": ["1", 0], "filename_prefix": "cpu-check",
                                                                    "format": "auto", "codec": "auto"}}})
            return {"device": "cpu", "guard": False, "image_audio_video_io": "passed",
                    "provider_calls": 0}
    finally:
        await process.stop()


@app.local_entrypoint()
def main():
    print(json.dumps(verify.remote(), ensure_ascii=False, indent=2))
