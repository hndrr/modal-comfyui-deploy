"""Run inside the split CPU container. Starts GPU for synthetic API smoke tests."""

import asyncio
import json
import time
import uuid

import aiohttp
import modal


async def main():
    origin = "http://127.0.0.1:8000"
    gpu = modal.Function.from_name("comfyui-split", "gpu_worker")
    async with aiohttp.ClientSession() as client:
        async def request(method, path, **kwargs):
            async with client.request(method, origin + path, **kwargs) as response:
                data = await response.json()
                assert response.status < 400, (path, response.status, data)
                return data

        sid = "verification-" + uuid.uuid4().hex
        events = []
        async with client.ws_connect(origin + "/api/ws?clientId=" + sid) as socket:
            async def read():
                async for message in socket:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        events.append(json.loads(message.data))
            reader = asyncio.create_task(read())
            started = time.monotonic()
            try:
                body = {"client_id": sid, "prompt": {
                    "1": {"class_type": "EmptyImage", "inputs": {
                        "width": 64, "height": 64, "batch_size": 1, "color": 3368601}},
                    "2": {"class_type": "SaveImage", "inputs": {
                        "images": ["1", 0], "filename_prefix": "split-verification/smoke"}}
                }}
                first, second = await asyncio.gather(*[
                    request("POST", "/api/prompt", json=body) for _ in range(2)])
                assert first["prompt_id"] != second["prompt_id"]
                pending = (await request("GET", "/api/queue"))["queue_pending"]
                assert pending, "Expected at least one pending prompt during GPU startup"
                cancelled = pending[-1][1]
                await request("POST", "/api/queue", json={"delete": [cancelled]})
                remaining = ({first["prompt_id"], second["prompt_id"]} - {cancelled}).pop()
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    history = await request("GET", "/api/history/" + remaining)
                    if history:
                        break
                    await asyncio.sleep(2)
                assert history, "Generation timeout"
                item = history[remaining]
                assert item["status"]["status_str"] == "success", item
                image = item["outputs"]["2"]["images"][0]
                async with client.get(origin + "/api/view", params=image) as response:
                    assert response.status == 200
                    assert len(await response.read()) > 0
                assert not await request("GET", "/api/history/" + cancelled)
                assert any(e.get("type") == "executing" and
                           e.get("data", {}).get("prompt_id") == remaining for e in events), events
                print(json.dumps({"event": "generation_verified", "job": remaining,
                                  "cancelled": cancelled, "seconds": time.monotonic() - started,
                                  "events": sorted({e["type"] for e in events})}), flush=True)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        for mode in ("legacy", "split"):
            await request("POST", "/split/mode", json={"mode": mode})
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                state = await request("GET", "/split/status")
                if state["mode"] == mode and not state["transitioning"]:
                    break
                await asyncio.sleep(2)
            else:
                raise AssertionError(("Mode switch timeout", state))
            stats = await request("GET", "/api/system_stats")
            device = stats["devices"][0]["type"]
            assert device == ("cuda" if mode == "legacy" else "cpu"), stats
            print(json.dumps({"event": "mode_verified", "mode": mode, "device": device}), flush=True)
        started = time.monotonic()
        while time.monotonic() - started < 120:
            stats = await gpu.get_current_stats.aio()
            assert stats.num_total_runners <= 1, stats
            if stats.num_total_runners == 0:
                print(json.dumps({"event": "gpu_zero", "seconds_after_return": time.monotonic() - started}), flush=True)
                return
            await asyncio.sleep(2)
        raise AssertionError("GPU did not scale to zero")


if __name__ == "__main__":
    asyncio.run(main())
