"""Run inside the split CPU container; exercises UI APIs without GPU calls.

This is an API acceptance check, not a substitute for browser interaction tests.
It writes only files under split-verification and preserves their evidence.
"""

import asyncio
import base64
import json
import time
import uuid

import aiohttp
import modal


async def main():
    origin = "http://127.0.0.1:8000"
    gpu = modal.Function.from_name("comfyui-split", "gpu_worker")
    started = time.monotonic()
    checks = 0
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    async with aiohttp.ClientSession() as client:
        async with client.ws_connect(origin + "/ws?clientId=idle-" + uuid.uuid4().hex) as socket:
            async def drain():
                async for _ in socket:
                    pass
            reader = asyncio.create_task(drain())
            try:
                while True:
                    stats = await gpu.get_current_stats.aio()
                    assert stats.num_total_runners == 0, str(stats)
                    for route in ("/", "/api/object_info", "/api/extensions", "/api/queue",
                                  "/api/history", "/api/system_stats", "/api/models/checkpoints"):
                        async with client.get(origin + route) as response:
                            assert response.status == 200, (route, response.status, await response.text())
                            await response.read()
                    form = aiohttp.FormData()
                    form.add_field("image", png, filename="idle.png", content_type="image/png")
                    form.add_field("subfolder", "split-verification")
                    form.add_field("overwrite", "true")
                    async with client.post(origin + "/api/upload/image", data=form) as response:
                        assert response.status == 200, await response.text()
                        uploaded = await response.json()
                    async with client.get(origin + "/api/view", params={
                        "filename": uploaded["name"], "subfolder": uploaded["subfolder"], "type": "input"
                    }) as response:
                        assert response.status == 200, await response.text()
                        assert await response.read()
                    workflow = {"nodes": [], "links": [], "version": 0.4,
                                "extra": {"verification_iteration": checks,
                                          "elapsed_seconds": round(time.monotonic() - started, 1)}}
                    route = "/api/userdata/workflows%2Fsplit-verification-idle.json?overwrite=true"
                    async with client.post(origin + route, json=workflow) as response:
                        assert response.status == 200, await response.text()
                    async with client.get(origin + route) as response:
                        assert response.status == 200, await response.text()
                        assert await response.json() == workflow
                    assert not socket.closed, "WebSocket disconnected"
                    checks += 1
                    elapsed = time.monotonic() - started
                    print(json.dumps({"elapsed_seconds": round(elapsed, 1),
                                      "iterations": checks, "gpu_runners": stats.num_total_runners}), flush=True)
                    if elapsed >= 600:
                        print(json.dumps({"event": "idle_verified", "elapsed_seconds": elapsed,
                                          "iterations": checks}), flush=True)
                        break
                    await asyncio.sleep(min(20, 600 - elapsed))
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
