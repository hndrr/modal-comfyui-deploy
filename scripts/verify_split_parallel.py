"""Verify four real generations on the deployed split app (billable L4 GPUs)."""

import asyncio
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import time
import uuid

import aiohttp
from dotenv import load_dotenv
import modal
from PIL import Image


async def main():
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    origin = await modal.Function.from_name("comfyui-split", "ui").get_web_url.aio()
    headers = {"Modal-Key": os.environ["MODAL_PROXY_KEY"], "Modal-Secret": os.environ["MODAL_PROXY_SECRET"]}
    graph = json.loads((root / "scripts/verify_split_gpu_workflow.json").read_text())
    graph["7"]["inputs"]["steps"] = 48  # Keep inference active across cold-start variation.
    graph["6"]["inputs"].update(width=1024, height=1024)
    graph["9"]["inputs"]["filename_prefix"] = "split-verification/parallel"
    run = uuid.uuid4().hex
    async with aiohttp.ClientSession(base_url=origin, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=180), raise_for_status=True) as client:
        async def get(path):
            async with client.get(path) as response:
                return await response.json()
        state = await get("/split/status")
        if state["mode"] != "split" or state["busy"] or state["candidate"] or state["transitioning"]:
            raise RuntimeError("Wait for an idle split deployment.")
        ids = []
        for seed in range(4):
            prompt = deepcopy(graph)
            prompt["7"]["inputs"]["seed"] = seed
            async with client.post("/api/prompt", json={"prompt": prompt, "gpu": "L4"},
                                   headers={"Idempotency-Key": f"parallel-{run}-{seed}"}) as response:
                receipt = await response.json()
            assert receipt["gpu"] == "L4", receipt
            ids.append(receipt["prompt_id"])
        print(json.dumps({"submitted": ids}), flush=True)
        queued_fourth = False
        history = {}
        deadline = time.monotonic() + 1200
        while len(history) < 4 and time.monotonic() < deadline:
            queue = await get("/api/queue")
            running = {r[1] for r in queue["queue_running"]}
            pending = {r[1] for r in queue["queue_pending"]}
            assert len(running) <= 3, running
            queued_fourth |= set(ids[:3]) <= running and ids[3] in pending
            for job_id in ids:
                if job_id not in history:
                    history.update(await get("/api/history/" + job_id))
            await asyncio.sleep(1)
        assert queued_fourth and set(history) == set(ids), (queued_fourth, list(history))
        times = []
        hashes = set()
        for seed, job_id in enumerate(ids):
            item = history[job_id]
            status = item["status"]
            assert status["status_str"] == "success", (job_id, status["status_str"])
            events = dict(status["messages"])
            times.append((events["execution_start"]["timestamp"], events["execution_success"]["timestamp"]))
            image = item["outputs"]["9"]["images"][0]
            assert image["subfolder"] == f"jobs/{job_id}/split-verification", image
            async with client.get("/api/view", params={k: image[k] for k in ("type", "filename", "subfolder")}) as response:
                data = await response.read()
            with Image.open(io.BytesIO(data)) as rendered:
                assert rendered.format == "PNG" and rendered.size == (1024, 1024)
                if "prompt" in rendered.info:
                    assert json.loads(rendered.info["prompt"])["7"]["inputs"]["seed"] == seed
                hashes.add(hashlib.sha256(rendered.tobytes()).hexdigest())
        overlap = min(end for _, end in times[:3]) - max(start for start, _ in times[:3])
        assert overlap > 0 and times[3][0] >= min(end for _, end in times[:3]), times
        assert len(hashes) == 4
        data_volume = modal.Volume.from_name("comfy-split-data")
        journal = json.loads(b"".join([chunk async for chunk in data_volume.read_file.aio("/state/controller.json")]))
        tasks = []
        for job_id in ids:
            call = modal.FunctionCall.from_id(journal["jobs"][job_id]["call_id"])
            nodes = list(await call.get_call_graph.aio())
            while nodes:
                node = nodes.pop()
                if node.function_call_id == call.object_id:
                    tasks.append(node.task_id)
                    break
                nodes.extend(node.children)
            else:
                raise AssertionError(f"Missing Modal execution record for {job_id}")
        assert all(tasks) and len(set(tasks[:3])) == 3, tasks
        print(json.dumps({"prompt_ids": ids, "three_way_overlap_seconds": overlap / 1000,
                          "distinct_first_three_containers": len(set(tasks[:3])),
                          "distinct_images": len(hashes)}), flush=True)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if (await get("/split/status"))["gpu"]["containers"] == 0:
                print("GPU containers returned to zero", flush=True)
                return
            await asyncio.sleep(5)
        raise TimeoutError("GPU containers still active")


if __name__ == "__main__":
    asyncio.run(main())
