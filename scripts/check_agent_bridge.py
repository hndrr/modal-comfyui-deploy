"""Explicit deployed Bridge transport smoke (starts one GPU workflow).

Uses the real custom node and native protocol with a fixture Mac peer. No Codex
CLI or model API is called. Credentials stay inside a short-lived Modal CPU job.
Run: ./scripts/modal.sh run scripts/check_agent_bridge.py
"""

import asyncio
import io
import os
import sys
import uuid
from pathlib import Path

import modal

if modal.is_local():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import comfyapp  # noqa: E402,F401 - shared dotenv resolution

app = modal.App("comfyui-agent-bridge-check")
image = modal.Image.debian_slim(python_version="3.12").pip_install("aiohttp==3.12.15", "pillow==11.3.0")
configuration = modal.Secret.from_dict({key: os.environ.get(key, "") for key in (
    "AMBIENT_COMFYUI_URL", "MODAL_PROXY_KEY", "MODAL_PROXY_SECRET")})
bridge_secret = modal.Secret.from_name(os.environ.get("AGENT_RUNTIME_SECRET_NAME") or "agent-runtime-secret",
                                       required_keys=["AGENT_RUNTIME_BRIDGE_TOKEN"])


@app.function(image=image, secrets=[configuration, bridge_secret], timeout=1200)
async def check():
    from aiohttp import ClientSession, ClientTimeout, WSMsgType
    from PIL import Image

    url = os.environ["AMBIENT_COMFYUI_URL"].rstrip("/")
    headers = {"Modal-Key": os.environ["MODAL_PROXY_KEY"], "Modal-Secret": os.environ["MODAL_PROXY_SECRET"],
               "Authorization": "Bearer " + os.environ["AGENT_RUNTIME_BRIDGE_TOKEN"]}
    prefix = "/agent_runtime/bridge"
    counts = {"jobs": 0, "inputs": 0, "artifacts": 0}
    output = io.BytesIO()
    Image.new("RGB", (64, 64), (36, 112, 180)).save(output, format="PNG")
    image_bytes = output.getvalue()
    job_id = None
    terminal = False
    async with ClientSession(headers=headers, timeout=ClientTimeout(total=660)) as client:
        print("Waiting for split startup and Ambient node refresh", flush=True)
        async with client.get(url + "/split/status") as response:
            response.raise_for_status()
            status = await response.json()
            if status["mode"] != "split" or status["busy"] or status["candidate"] or status["transitioning"]:
                raise RuntimeError("Bridge smoke requires an idle split deployment")
        print("Split ready; connecting fixture Bridge peer", flush=True)
        async with client.ws_connect(url + prefix + "/ws", heartbeat=15, max_msg_size=20 * 1024 * 1024) as socket:
            catalog = {"models": [], "skills": [], "revision": "transport-smoke", "auth": "authenticated"}
            await socket.send_json({"type": "hello", "version": 1,
                                    "worker_id": "transport-smoke-" + uuid.uuid4().hex, "catalog": catalog})
            if await socket.receive_json(timeout=20) != {"type": "ready", "version": 1}:
                raise RuntimeError("Bridge handshake failed")

            async def mac_peer():
                async for message in socket:
                    if message.type != WSMsgType.TEXT:
                        continue
                    import json
                    job = json.loads(message.data)
                    if job.get("type") != "job":
                        continue
                    counts["jobs"] += 1
                    if job["spec"]["kind"] == "catalog":
                        await socket.send_json({"type": "result", "id": job["id"], "result": {"catalog": catalog}})
                        continue
                    if job["spec"]["kind"] != "imagegen":
                        raise RuntimeError("Unexpected Bridge operation")
                    route = url + prefix + "/jobs/" + job["id"]
                    for item in job["files"]:
                        async with client.get(route + "/inputs/" + item["id"]) as response:
                            response.raise_for_status()
                            data = await response.read()
                        if item.get("role") == "media":
                            Image.open(io.BytesIO(data)).verify()
                        counts["inputs"] += 1
                    async with client.post(route + "/artifacts?name=fixture.png", data=image_bytes) as response:
                        response.raise_for_status()
                        artifact = await response.json()
                    counts["artifacts"] += 1
                    await socket.send_json({"type": "result", "id": job["id"], "result": {
                        "response": "Bridge transport verified", "exit_code": 0, "stderr": "",
                        "raw_output": "x" * (2 * 1024 * 1024), "used_prompt": job["spec"]["prompt"],
                        "artifacts": [artifact["id"]]}})

            peer = asyncio.create_task(mac_peer())
            try:
                async with client.get(url + "/object_info/AgentRuntimeBridgeImageGen") as response:
                    response.raise_for_status()
                    node = (await response.json())["AgentRuntimeBridgeImageGen"]
                inputs = {}
                for name, definition in node["input"]["required"].items():
                    options = definition[1] if len(definition) > 1 else {}
                    inputs[name] = options.get("default", definition[0][0] if isinstance(definition[0], list) else "")
                inputs.update(prompt="Verify the Bridge image transport using a fixture.", images=["1", 0],
                              timeout_seconds=120, cache_mode="always_run")
                prompt = {"1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 0}},
                          "2": {"class_type": "AgentRuntimeBridgeImageGen", "inputs": inputs},
                          "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "_bridge_smoke/transport"}}}
                async with client.post(url + "/prompt", json={"prompt": prompt},
                                       headers={"Idempotency-Key": "bridge-smoke-" + uuid.uuid4().hex}) as response:
                    response.raise_for_status()
                    job_id = (await response.json())["prompt_id"]
                print("Bridge workflow accepted: " + job_id, flush=True)
                async with asyncio.timeout(480):
                    while True:
                        if peer.done():
                            await peer
                            raise RuntimeError("Bridge peer disconnected before completion")
                        async with client.get(url + "/history/" + job_id) as response:
                            response.raise_for_status()
                            history = (await response.json()).get(job_id)
                        if history:
                            terminal = True
                            if history["status"]["status_str"] != "success":
                                raise RuntimeError(str(history["status"]))
                            saved = history["outputs"]["3"]["images"][0]
                            async with client.get(url + "/view", params=saved) as response:
                                response.raise_for_status()
                                actual = Image.open(io.BytesIO(await response.read())).convert("RGB")
                            if actual.size != (64, 64) or actual.getpixel((0, 0)) != (36, 112, 180):
                                raise RuntimeError("Saved output differs from the uploaded Bridge image")
                            if counts["inputs"] < 1 or counts["artifacts"] != 1:
                                raise RuntimeError("Bridge file transfer was not exercised")
                            return {"status": "passed", "prompt_id": job_id, **counts, "output": saved,
                                    "model_api_called": False}
                        await asyncio.sleep(1)
            finally:
                if job_id and not terminal:
                    async with client.post(url + "/jobs/" + job_id + "/cancel", json={}) as response:
                        response.raise_for_status()
                peer.cancel()
                await asyncio.gather(peer, return_exceptions=True)


@app.local_entrypoint()
def main():
    print(check.remote())
