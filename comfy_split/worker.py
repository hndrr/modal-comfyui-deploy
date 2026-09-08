"""One Modal function owns every GPU operation, including legacy sessions."""

import asyncio
import hmac
import json
import os
import queue
import shutil
import time
from pathlib import Path

import modal
from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from comfy_split.proxy import proxy
from comfy_split.runtime import ComfyProcess
from comfy_split.state import write_json

process = ComfyProcess("gpu", 8188)


async def run_worker(spec, events, commands, volumes):
    session_id = spec["id"]
    result_path = Path("/results") / (session_id + ".json")
    started_path = Path("/results") / (session_id + ".started.json")
    started = time.monotonic()

    async def emit(value):
        try:
            await events.put.aio(value, block=False, partition=session_id)
        except queue.Full:
            # Preview/progress is best effort. The result journal is authoritative.
            pass

    async def control():
        return await commands.get_many.aio(20, block=False, partition=session_id)

    result = None
    async with ClientSession(timeout=ClientTimeout(total=None), auto_decompress=False) as client:
        try:
            if process.version is not None and process.version != spec["environment"]:
                await process.stop()
            for name in ("environment", "input", "models", "user", "results", "output"):
                # Environments are immutable. Native libraries imported from a
                # venv stay open for the process lifetime and prevent reload.
                if name == "environment" and process.version == spec["environment"]:
                    continue
                try:
                    await volumes[name].reload.aio()
                except RuntimeError as error:
                    if "open files" not in str(error):
                        raise
                    # Some nodes keep model/input files mapped after execution.
                    # Close those handles before refreshing the Volume snapshot.
                    await process.stop()
                    await volumes[name].reload.aio()
            if result_path.exists():
                return json.loads(result_path.read_text())
            if started_path.exists():
                # Modal can replay inputs after preemption independently of the
                # configured application retries. Never rerun ambiguous effects.
                result = {"status": "unknown", "error": "GPUの実行開始記録がありますが結果がありません。自動再実行はしません。"}
                write_json(result_path, result)
                await volumes["results"].commit.aio()
                return result
            write_json(started_path, {"id": session_id, "at": time.time(), "operation": spec["operation"]})
            await volumes["results"].commit.aio()
            await process.start(spec["environment"])
            print(json.dumps({"event": "gpu_ready", "id": session_id,
                              "seconds": time.monotonic() - started}))
            if spec["operation"] == "validate":
                catalog = await process.catalog(client)
                if catalog.get("import_failures"):
                    raise RuntimeError("GPUで読み込めないノード: " + ", ".join(catalog["import_failures"]))
                result = {"catalog": catalog, "status": "completed"}
            elif spec["operation"] == "legacy":
                result = await legacy(spec, client, control, emit)
            else:
                result = await asyncio.wait_for(generate(spec, client, control, emit),
                    timeout=int(os.environ.get("SPLIT_GENERATION_TIMEOUT", "1800")))
        except Exception as error:
            # Do not reuse a server whose execution/queue state is uncertain.
            await process.stop()
            result = {"status": "failed", "error": str(error)}
        # Closing model/asset files before reload on the next invocation is the
        # subprocess's responsibility. Incompatible reloads fail, never run stale inputs.
        await asyncio.to_thread(process.archive_temp)
        if result.get("history"):
            result["history"]["outputs"] = process.durable_outputs(result["history"].get("outputs", {}))
        await volumes["output"].commit.aio()
        if spec["operation"] == "legacy":
            await volumes["user"].commit.aio()
        result["seconds"] = time.monotonic() - started
        write_json(result_path, result)
        await volumes["results"].commit.aio()
        await emit({"type": "result_ready"})
        print(json.dumps({"event": "gpu_finished", "id": session_id,
                          "seconds": result["seconds"], "status": result["status"]}))
        return result


async def generate(spec, client, control, emit):
    job_id = spec["id"]
    body = dict(spec["body"], prompt_id=job_id, client_id=job_id)
    async with client.ws_connect(process.url + "/ws?clientId=" + job_id, compress=0,
                                 max_msg_size=0) as socket:
        async with client.post(process.url + "/prompt", json=body) as response:
            accepted = await response.json()
            if response.status != 200:
                return {"status": "failed", "error": accepted}
            if accepted.get("prompt_id") != job_id:
                raise RuntimeError("ComfyUI did not preserve prompt_id")

        async def relay():
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    event = json.loads(message.data)
                    # Final completion is sent only after files have been committed.
                    if event.get("type") == "status":
                        continue
                    if event.get("type") == "executing" and event.get("data", {}).get("node") is None:
                        continue
                    if event.get("type") == "executed":
                        event["data"]["output"] = process.durable_outputs(event["data"].get("output", {}))
                    await emit({"type": "event", "event": event})
                elif message.type == WSMsgType.BINARY and len(message.data) < 512 * 1024:
                    await emit({"type": "preview", "data": message.data})

        relay_task = asyncio.create_task(relay())
        try:
            while True:
                for command in await control():
                    if command["type"] == "interrupt":
                        async with client.post(process.url + "/interrupt", json={}) as response:
                            response.raise_for_status()
                async with client.get(process.url + "/history/" + job_id) as response:
                    response.raise_for_status()
                    history = (await response.json()).get(job_id)
                if history:
                    status = history.get("status", {}).get("status_str")
                    messages = history.get("status", {}).get("messages", [])
                    interrupted = any(m[0] == "execution_interrupted" for m in messages)
                    return {"status": "cancelled" if interrupted else
                            "completed" if status == "success" else "failed", "history": history}
                if process.process.returncode is not None:
                    raise RuntimeError("ComfyUI exited while executing the workflow")
                await asyncio.sleep(0.3)
        finally:
            relay_task.cancel()
            await asyncio.gather(relay_task, return_exceptions=True)


async def legacy(spec, client, control, emit):
    """Authenticated, streaming tunnel; no public unauthenticated ComfyUI port."""
    token = spec["token"]
    admission = asyncio.Lock()
    accepting = True

    async def handler(request):
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            raise web.HTTPForbidden()
        # Manager mutations must always go through the versioned CPU updater.
        path = request.path.removeprefix("/api")
        if "manager" in path or path.startswith(("/customnode", "/v2/customnode", "/snapshot", "/v2/snapshot")):
            return web.json_response({"error": "ノード更新は分離モードで実行してください。"}, status=409)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            async with admission:
                if not accepting:
                    return web.json_response({"error": "モード切替中です。"}, status=409)
                return await proxy(request, client, process.url)
        return await proxy(request, client, process.url)

    app = web.Application(client_max_size=1024 ** 3)
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8189).start()
    try:
        async with modal.forward(8189) as tunnel:
            await emit({"type": "legacy_ready", "url": tunnel.url})
            last_heartbeat = time.monotonic()
            last_ready = last_heartbeat
            stopping = False
            while True:
                if time.monotonic() - last_ready >= 15:
                    # CPU can restart between consuming the event and persisting
                    # the URL. Reannounce without opening a second GPU session.
                    await emit({"type": "legacy_ready", "url": tunnel.url})
                    last_ready = time.monotonic()
                for command in await control():
                    if command["type"] == "heartbeat":
                        last_heartbeat = time.monotonic()
                    if command["type"] == "stop":
                        stopping = True
                async with admission:
                    if stopping or time.monotonic() - last_heartbeat > 90:
                        accepting = False
                        async with client.get(process.url + "/queue") as response:
                            response.raise_for_status()
                            pending = await response.json()
                        if not any(pending.values()):
                            break
                await asyncio.sleep(1)
        async with client.get(process.url + "/history") as response:
            history = await response.json()
        for item in history.values():
            item["outputs"] = process.durable_outputs(item.get("outputs", {}))
        await process.stop()
        # CPU server is stopped throughout legacy mode, so there is one user writer.
        shutil.copytree(process.root / "user", "/data/user", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.db", "*.db-shm", "*.db-wal", "__manager"))
        return {"status": "completed", "legacy_history": history}
    finally:
        await runner.cleanup()
