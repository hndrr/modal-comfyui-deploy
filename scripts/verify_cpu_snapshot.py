"""Read-only deployed CPU cold-start check. Does not submit GPU work.

Run with MODAL_PROFILE set and proxy credentials in the checkout's .env.
Close ComfyUI/Studio's active sessions first so the CPU can scale to zero.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import aiohttp
from dotenv import load_dotenv
import modal


async def main(output, boots, expected_prompt=None, until_restored=False):
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    origin = os.environ["AMBIENT_COMFYUI_URL"].rstrip("/")
    headers = {"Modal-Key": os.environ["MODAL_PROXY_KEY"],
               "Modal-Secret": os.environ["MODAL_PROXY_SECRET"]}
    function = modal.Function.from_name("comfyui-split", "ui")
    results = json.loads(output.read_text()) if output.exists() else []
    for _ in range(boots):
        deadline = time.monotonic() + 300
        while True:
            stats = await function.get_current_stats.aio()
            if not stats.num_total_runners and not stats.backlog:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("CPU is not idle; close active browser/Bridge sessions")
            await asyncio.sleep(5)
        print("CPU runners=0 backlog=0; starting cold request", flush=True)
        started = time.monotonic()
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=600)) as client:
            async def get(path):
                async with client.get(origin + path) as response:
                    response.raise_for_status()
                    return await response.json()
            while True:
                status = await get("/split/startup")
                if status["failed"]:
                    raise RuntimeError(status)
                if status["ready"]:
                    break
                await asyncio.sleep(1)
            ready_seconds = time.monotonic() - started
            objects, queue, extensions = await asyncio.gather(*[get(path) for path in
                ("/object_info", "/queue", "/extensions")])
            assert not queue["queue_running"] and not queue["queue_pending"], queue
            assert {"MiniMaxH3ImageToVideo", "JevInterpret", "AgentRuntimeBridgeText"} <= objects.keys()
            async with client.get(origin + "/") as response:
                assert response.status == 200 and len(await response.read()) > 100
            sid = "snapshot-check-" + uuid.uuid4().hex
            async with client.ws_connect(origin + "/ws", params={"clientId": sid}) as socket:
                message = await socket.receive_json(timeout=10)
                assert message["data"]["sid"] == sid, message
            async with client.post(origin + "/_split/restore") as response:
                assert response.status == 404, "Internal restore endpoint must not be public"
            if expected_prompt:
                history = await get("/history/" + expected_prompt)
                assert history[expected_prompt]["status"]["completed"], history
        snapshot = status["snapshot"]
        restored = any(old["status"]["snapshot"]["initialization_id"] == snapshot["initialization_id"]
                       and old["status"]["snapshot"]["container_id"] != snapshot["container_id"]
                       and old["status"]["snapshot"]["restoration_id"] != snapshot["restoration_id"]
                       for old in results)
        result = {"ready_seconds": round(ready_seconds, 3), "total_seconds": round(time.monotonic() - started, 3),
            "status": status, "node_count": len(objects), "extension_count": len(extensions),
            "schema_hash": hashlib.sha256(json.dumps(objects, sort_keys=True).encode()).hexdigest(),
            "websocket": True, "queue_empty": True, "checked_at": time.time(),
            "restored_previous_initialization": restored, "completed_prompt": expected_prompt}
        results.append(result)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(result), flush=True)
        if until_restored and restored:
            break
    if until_restored and not results[-1].get("restored_previous_initialization"):
        raise RuntimeError("All boots succeeded, but no previously recorded snapshot was reused")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--boots", type=int, default=1)
    parser.add_argument("--expected-prompt")
    parser.add_argument("--until-restored", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.output, args.boots, args.expected_prompt, args.until_restored))
