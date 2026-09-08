"""CPU-only HTTP control plane. GPU calls occur only in explicit operations."""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
import traceback
import uuid
from pathlib import Path

import modal
from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from comfy_split.proxy import proxy
from comfy_split.runtime import (
    ComfyProcess, create_environment, environment_path, initialize_environment,
)
from comfy_split.state import ACTIVE, Journal, write_json

log = logging.getLogger(__name__)


def api_path(path):
    return path[4:] if path.startswith("/api/") else path


def manager_path(path):
    path = path.removeprefix("/v2")
    return path.startswith(("/manager/", "/customnode/", "/snapshot/", "/comfyui_manager/"))


class Controller:
    def __init__(self, worker, events, commands, volumes, root=Path("/state")):
        self.worker, self.events, self.commands, self.volumes = worker, events, commands, volumes
        self.journal = Journal(root)
        self.lock = asyncio.Lock()
        self.cpu = ComfyProcess("cpu", 8187)
        self.candidate = ComfyProcess("candidate", 8190)
        self.sockets = {}
        self.task = None
        self.apply_task = None
        self.client = None
        self.candidate_relays = {}
        self.gpu_stats_lock = asyncio.Lock()
        self.gpu_stats = None
        self.gpu_stats_expiry = 0

    async def gpu_status(self):
        # Control-plane metadata only: never invoke the GPU to display its state.
        async with self.gpu_stats_lock:
            if time.monotonic() >= self.gpu_stats_expiry:
                try:
                    stats = await asyncio.wait_for(self.worker.get_current_stats.aio(), timeout=3)
                    self.gpu_stats = {"containers": stats.num_total_runners,
                                      "checked_at": time.time()}
                except Exception:
                    log.warning("Could not read GPU container count", exc_info=True)
                    self.gpu_stats = {"containers": None, "checked_at": None}
                self.gpu_stats_expiry = time.monotonic() + 5
            result = dict(self.gpu_stats)
        data = self.journal.data
        count = result["containers"]
        if count is None:
            phase = "unknown"
        elif count == 0:
            phase = "starting" if self.journal.busy() or data["session"] else "stopped"
        elif data["session"] and data["session"].get("stopping"):
            phase = "stopping"
        elif data["mode"] == "legacy":
            phase = "legacy"
        elif data["session"]:
            phase = "maintenance"
        elif self.journal.busy():
            phase = "active"
        else:
            phase = "stopping"
        return dict(result, phase=phase)

    async def persist(self):
        self.journal.save()
        await self.volumes["state"].commit.aio()

    async def start(self, app):
        self.client = ClientSession(timeout=ClientTimeout(total=None), auto_decompress=False)
        await asyncio.to_thread(initialize_environment)
        await self.volumes["environment"].commit.aio()
        await self.volumes["user"].commit.aio()
        self.journal.recover()
        candidate = self.journal.data["candidate"]
        session = self.journal.data["session"]
        if candidate and candidate["status"] in {"creating", "validating"}:
            if not (session and session.get("operation") == "validate"):
                candidate.update(status="failed", error="環境更新中にCPUが再起動しました。旧環境を維持しています。")
        await self.persist()
        if self.journal.data["mode"] == "split":
            await self.cpu.start(self.journal.data["environment"], cpu=True)
        self.task = asyncio.create_task(self.dispatch())
        if candidate and candidate["status"] == "validating" and session:
            self.apply_task = asyncio.create_task(self.apply_environment(resume=True))

    async def close(self, app):
        await self.close_candidate_relays()
        for task in (self.task, self.apply_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.cpu.stop()
        await self.candidate.stop()
        if self.client:
            await self.client.close()

    async def broadcast(self, event, client_id=None):
        targets = list(self.sockets.items())
        for sid, sockets in targets:
            if client_id and sid != client_id:
                continue
            for socket in list(sockets):
                try:
                    if isinstance(event, bytes):
                        await asyncio.wait_for(socket.send_bytes(event), 2)
                    else:
                        await asyncio.wait_for(socket.send_json(event), 2)
                except (ConnectionError, asyncio.TimeoutError):
                    sockets.discard(socket)

    async def status(self):
        count = sum(len(items) for items in self.journal.queue().values())
        await self.broadcast({"type": "status", "data": {
            "status": {"exec_info": {"queue_remaining": count}}}})

    async def command(self, session_id, type_):
        await self.commands.put.aio({"type": type_}, partition=session_id)

    async def spawn(self, record, operation):
        # Persist intent BEFORE spawn. A crash between spawn and call-id commit
        # leaves an unknown job, which recovery must never blindly resubmit.
        record["status"] = "dispatching"
        await self.persist()
        spec = {"id": record["id"], "operation": operation,
                "environment": record["environment"]}
        for key in ("body", "token"):
            if key in record:
                spec[key] = record[key]
        call = await self.worker.spawn.aio(spec)
        record["call_id"] = call.object_id
        record["status"] = "running"
        await self.persist()

    async def read_result(self, record):
        await self.volumes["results"].reload.aio()
        path = Path("/results") / (record["id"] + ".json")
        if path.exists():
            return json.loads(path.read_text())
        if not record.get("call_id"):
            return None
        call = modal.FunctionCall.from_id(record["call_id"])
        try:
            return await call.get.aio(timeout=0)
        except (TimeoutError, modal.exception.TimeoutError):
            return None
        except Exception as error:
            # FunctionCall failure is terminal; a network failure is not.
            if isinstance(error, (modal.exception.FunctionTimeoutError,
                                  modal.exception.RemoteError)) or any(
                    frame.filename.startswith("<ta-") for frame in traceback.extract_tb(error.__traceback__)):
                return {"status": "failed", "error": str(error)}
            raise

    async def drain_events(self, record):
        for event in await self.events.get_many.aio(100, block=False, partition=record["id"]):
            if event["type"] == "legacy_ready":
                record["url"] = event["url"]
                await self.persist()
            elif event["type"] in {"event", "preview"}:
                data = event.get("event", event.get("data"))
                # Files referenced in executed events may not be committed yet.
                if isinstance(data, dict) and data.get("type") == "executed":
                    record.setdefault("deferred_events", []).append(data)
                else:
                    await self.broadcast(data, record.get("body", {}).get("client_id"))

    async def finish(self, record, result):
        await self.volumes["output"].reload.aio()
        record.update({key: value for key, value in result.items()
                       if key in {"status", "error", "history", "seconds"}})
        record["finished_at"] = time.time()
        if not record.get("history"):
            record["history"] = {
                "prompt": [record.get("number", 0), record["id"], record.get("body", {}).get("prompt", {}), {}, []],
                "outputs": {}, "status": {"status_str": "error", "completed": False,
                "messages": [["execution_error", {"prompt_id": record["id"],
                               "exception_message": str(record.get("error", "Execution failed"))}]]},
            }
        await self.persist()
        for event in record.pop("deferred_events", []):
            await self.broadcast(event, record.get("body", {}).get("client_id"))
        if record["status"] == "failed":
            await self.broadcast({"type": "execution_error", "data": {
                "prompt_id": record["id"], "node_id": "", "node_type": "",
                "executed": [], "exception_type": "RemoteExecutionError",
                "exception_message": str(record.get("error", "生成に失敗しました")),
                "traceback": []}}, record.get("body", {}).get("client_id"))
        await self.broadcast({"type": "executing", "data": {"node": None, "prompt_id": record["id"]}})
        await self.status()

    async def dispatch(self):
        while True:
            try:
                async with self.lock:
                    session = self.journal.data["session"]
                    if session:
                        await self.drain_events(session)
                        if session.get("call_id") or session.get("status") == "dispatching":
                            if session.get("operation") == "legacy" and not session.get("stopping"):
                                await self.command(session["id"], "heartbeat")
                            result = await self.read_result(session)
                            if result:
                                session["result"] = result
                                session["status"] = result["status"]
                                await self.persist()
                                if session["operation"] == "legacy":
                                    await self.end_legacy(result)
                    elif self.journal.data["mode"] == "split" and not self.journal.data["candidate"]:
                        running = next((j for j in self.journal.data["jobs"].values()
                                        if j["status"] in ACTIVE - {"queued"}), None)
                        if running:
                            await self.drain_events(running)
                            result = await self.read_result(running)
                            if result and not (result.get("status") == "unknown" and running["status"] == "unknown"):
                                await self.finish(running, result)
                        else:
                            job = self.journal.next_job()
                            if job:
                                await self.volumes["input"].commit.aio()
                                await self.volumes["models"].reload.aio()
                                await self.spawn(job, "generate")
                                await self.status()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Dispatcher failed; preserving journal for recovery")
                await asyncio.sleep(2)

    async def end_legacy(self, result):
        await self.volumes["output"].reload.aio()
        await self.volumes["user"].reload.aio()
        for job_id, history in result.get("legacy_history", {}).items():
            if job_id not in self.journal.data["jobs"]:
                self.journal.data["jobs"][job_id] = {
                    "id": job_id, "number": history["prompt"][0],
                    "body": {"prompt": history["prompt"][2]}, "status": "completed",
                    "history": history, "created_at": time.time(), "error": None}
        await self.cpu.start(self.journal.data["environment"], cpu=True)
        self.journal.data.update(mode="split", session=None)
        await self.persist()
        await self.broadcast({"type": "split_mode", "data": {"mode": "split"}})

    async def websocket(self, request):
        sid = request.query.get("clientId") or str(uuid.uuid4())
        socket = web.WebSocketResponse(compress=False, max_msg_size=0)
        await socket.prepare(request)
        self.sockets.setdefault(sid, set()).add(socket)
        if self.journal.data["candidate"] and self.journal.data["candidate"]["status"] == "editing":
            await self.attach_candidate(sid)
        count = sum(len(q) for q in self.journal.queue().values())
        await socket.send_json({"type": "status", "data": {
            "sid": sid, "status": {"exec_info": {"queue_remaining": count}}}})
        try:
            # Relay CPU custom-node events, while queue/status belongs to us.
            async with self.client.ws_connect(self.cpu.url + "/ws", params={"clientId": sid},
                                              compress=0) as upstream:
                async def receive():
                    async for message in upstream:
                        if message.type == WSMsgType.TEXT:
                            data = json.loads(message.data)
                            if data.get("type") != "status":
                                await socket.send_json(data)
                task = asyncio.create_task(receive())
                try:
                    async for message in socket:
                        if message.type == WSMsgType.TEXT:
                            await upstream.send_str(message.data)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            self.sockets[sid].discard(socket)
        return socket

    async def cancel(self, ids):
        for job_id in ids:
            job = self.journal.data["jobs"].get(job_id)
            if not job:
                continue
            if job["status"] == "queued":
                job["status"] = "cancelled"
            elif job["status"] in {"dispatching", "running"}:
                await self.command(job_id, "interrupt")
        await self.persist()
        await self.status()

    async def mode(self, request):
        desired = (await request.json()).get("mode")
        async with self.lock:
            if desired == self.journal.data["mode"]:
                return web.json_response({"mode": desired})
            if desired == "legacy":
                self.journal.assert_idle()
                await self.volumes["input"].commit.aio()
                await self.volumes["user"].commit.aio()
                await self.cpu.stop()
                session = {"id": str(uuid.uuid4()), "operation": "legacy",
                           "environment": self.journal.data["environment"],
                           "token": secrets.token_urlsafe(32), "call_id": None}
                self.journal.data.update(mode="legacy", session=session)
                await self.spawn(session, "legacy")
            elif desired == "split":
                session = self.journal.data["session"]
                if not session or not session.get("url"):
                    raise ValueError("GPUの起動完了を待ってから切り替えてください。")
                async with self.client.get(session["url"] + "/queue", headers={
                    "Authorization": "Bearer " + session["token"]}) as response:
                    response.raise_for_status()
                    if any((await response.json()).values()):
                        raise ValueError("実行中または待機中の生成があります。")
                session["stopping"] = True
                await self.command(session["id"], "stop")
                await self.persist()
            else:
                raise ValueError("Invalid mode")
        return web.json_response({"mode": desired, "transitioning": True}, status=202)

    async def ensure_candidate(self, *, restore_image_browsing=False):
        if restore_image_browsing and self.journal.data["candidate"]:
            raise ValueError("既存の環境更新を完了または破棄してから修復してください。")
        if not self.journal.data["candidate"]:
            self.journal.assert_idle()
            # Reserve before any subprocess/network operation can yield.
            self.journal.data["candidate"] = {"status": "creating", "version": None}
            await self.persist()
            options = {"restore_image_browsing": True} if restore_image_browsing else {}
            version = await asyncio.to_thread(create_environment, self.journal.data["environment"], **options)
            self.journal.data["candidate"] = {"status": "editing", "version": version}
            await self.volumes["environment"].commit.aio()
            await self.persist()
        candidate = self.journal.data["candidate"]
        if candidate["status"] != "editing":
            raise ValueError("環境更新処理中です。状態画面を確認してください。")
        await self.candidate.start(candidate["version"], cpu=True, manager=True)
        for sid in self.sockets:
            await self.attach_candidate(sid)

    async def close_candidate_relays(self):
        tasks = list(self.candidate_relays.values())
        self.candidate_relays.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def attach_candidate(self, sid):
        if sid in self.candidate_relays and not self.candidate_relays[sid].done():
            return
        socket = await self.client.ws_connect(self.candidate.url + "/ws",
                                              params={"clientId": sid}, compress=0)
        async def relay():
            try:
                async for message in socket:
                    if message.type == WSMsgType.TEXT:
                        event = json.loads(message.data)
                        if event.get("type") != "status":
                            await self.broadcast(event, sid)
            finally:
                await socket.close()
        self.candidate_relays[sid] = asyncio.create_task(relay())

    async def apply_environment(self, resume=False):
        candidate = self.journal.data["candidate"]
        try:
            # Manager may still have an installation queue executing.
            if not resume:
                async with self.client.get(self.candidate.url + "/v2/manager/queue/status") as response:
                    if response.status != 200:
                        raise RuntimeError("Manager queue status could not be verified")
                    queue = await response.json()
                    if "is_processing" not in queue:
                        raise RuntimeError("Unrecognized Manager queue status")
                    if queue["is_processing"] or queue.get("pending_count", 0):
                        raise RuntimeError("Managerのインストール完了を待ってください。")
            version = candidate["version"]
            await self.close_candidate_relays()
            await self.candidate.stop()
            python = str(environment_path(version) / "venv/bin/python")
            check = await asyncio.create_subprocess_exec(python, "-m", "comfy_split.check_environment",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            output, _ = await check.communicate()
            if check.returncode:
                raise RuntimeError(output.decode(errors="replace"))
            await self.candidate.start(version, cpu=True, manager=True)
            cpu_catalog = await self.candidate.catalog(self.client)
            await self.candidate.stop()
            await self.volumes["environment"].commit.aio()
            await self.volumes["models"].commit.aio()
            async with self.lock:
                if resume:
                    session = self.journal.data["session"]
                else:
                    session = {"id": str(uuid.uuid4()), "operation": "validate",
                               "environment": version, "call_id": None}
                    self.journal.data["session"] = session
                    await self.spawn(session, "validate")
            while "result" not in session:
                await asyncio.sleep(0.5)
            result = session["result"]
            if result["status"] != "completed":
                raise RuntimeError(result.get("error", "GPU validation failed"))
            catalog = result["catalog"]
            catalog["cpu_nodes"] = cpu_catalog["nodes"]
            write_json(environment_path(version) / "catalog.json", catalog)
            write_json(environment_path(version) / "validated.json", {"at": time.time()})
            await self.volumes["environment"].commit.aio()
            await self.cpu.start(version, cpu=True)
            async with self.lock:
                self.journal.data.update(environment=version, candidate=None, session=None)
                await self.persist()
            await self.broadcast({"type": "split_environment", "data": {"status": "ready"}})
        except Exception as error:
            log.exception("Environment validation failed")
            async with self.lock:
                candidate["status"] = "failed"
                candidate["error"] = str(error)
                # Keep unresolved GPU calls reserved; never free the slot blindly.
                session = self.journal.data["session"]
                if session and session.get("result"):
                    self.journal.data["session"] = None
                await self.persist()
            await self.cpu.start(self.journal.data["environment"], cpu=True)

    async def manager(self, request, path):
        body = await request.read() if request.can_read_body else None
        if path.endswith("/queue/task") and body:
            task = json.loads(body)
            if task.get("kind") in {"update-comfyui", "update_comfyui"}:
                return web.json_response({"error": "ComfyUI本体の更新は再デプロイが必要です。"}, status=409)
        # ComfyUI core upgrades are deployments, not node-environment mutations.
        if "update_comfyui" in path or "comfyui/update" in path or "comfyui_switch_version" in path:
            return web.json_response({"error": "ComfyUI本体は固定バージョンです。再デプロイで更新します。"}, status=409)
        if path.endswith(("/reboot", "/restart")):
            async with self.lock:
                if self.journal.data["candidate"]:
                    if self.apply_task and not self.apply_task.done():
                        return web.json_response({"status": "validating"}, status=202)
                    self.journal.data["candidate"]["status"] = "validating"
                    await self.persist()
                    self.apply_task = asyncio.create_task(self.apply_environment())
                else:
                    if self.journal.busy():
                        raise ValueError("生成中は再起動できません。")
                    await self.cpu.stop()
                    await self.cpu.start(self.journal.data["environment"], cpu=True)
            return web.json_response({"status": "restarting"}, status=202)
        # Manager has some GET mutations too; default unknown routes to staging.
        readonly = (request.method == "POST" and path.endswith((
            "/import_fail_info", "/import_fail_info_bulk"))) or (
            request.method == "GET" and any(path.endswith(suffix) for suffix in (
            "/getlist", "/getmappings", "/version", "/queue/status", "/notice",
            "/get_unresolved", "/get_installed", "/fetch_updates", "/installed",
            "/is_legacy_manager_ui", "/queue/history", "/queue/history_list",
            "/channel_url_list", "/db_mode", "/policy/update", "/get_current")))
        async with self.lock:
            if not readonly:
                await self.ensure_candidate()
            candidate = self.journal.data["candidate"]
            if candidate and candidate["status"] == "editing":
                await self.candidate.start(candidate["version"], cpu=True, manager=True)
                origin = self.candidate.url
            else:
                origin = self.cpu.url
            return await proxy(request, self.client, origin, body=body)

    async def handle(self, request):
        path = api_path(request.path)
        try:
            if path.startswith("/_split/"):
                raise web.HTTPNotFound()
            if path == "/view" and request.query.get("type") == "temp":
                # Previously persisted temp references remain readable after the
                # normal ComfyUI startup cleanup has been restored.
                root = Path("/data/output/.split-temp/temp").resolve()
                file = (root / request.query.get("subfolder", "") / request.query.get("filename", "")).resolve()
                if not file.is_relative_to(root):
                    raise web.HTTPForbidden()
                if file.is_file():
                    return web.FileResponse(file)
            if path in {"/split/status", "/modal-control/v1/status"}:
                data = self.journal.data
                return web.json_response({"api_version": 1, "mode": data["mode"], "environment": data["environment"],
                    "gpu": await self.gpu_status(),
                    "candidate": data["candidate"], "busy": self.journal.busy(),
                    "transitioning": bool(data["session"] and (
                        not data["session"].get("url") or data["session"].get("stopping"))),
                    "unknown_jobs": [j["id"] for j in data["jobs"].values() if j["status"] == "unknown"]})
            if path == "/split/mode" and request.method == "POST":
                return await self.mode(request)
            if path == "/split/environment/discard" and request.method == "POST":
                async with self.lock:
                    if self.journal.data["session"] or (self.apply_task and not self.apply_task.done()):
                        raise ValueError("更新処理の完了を待ってください。")
                    await self.close_candidate_relays()
                    await self.candidate.stop()
                    self.journal.data["candidate"] = None
                    await self.persist()
                return web.json_response({"status": "discarded"})
            if path == "/split/environment/apply" and request.method == "POST":
                async with self.lock:
                    await self.ensure_candidate()
                return await self.manager(request, "/manager/reboot")
            if path == "/split/environment/repair-image-browsing" and request.method == "POST":
                async with self.lock:
                    await self.ensure_candidate(restore_image_browsing=True)
                return await self.manager(request, "/manager/reboot")
            if self.journal.data["mode"] == "legacy":
                session = self.journal.data["session"]
                if not session or not session.get("url") or session.get("stopping"):
                    return web.json_response({"error": "GPUの起動・モード切替中です。"}, status=503,
                                             headers={"Retry-After": "2"})
                if path == "/extensions":
                    return await self.extensions(session["url"], session["token"])
                return await proxy(request, self.client, session["url"], session["token"])
            if path == "/ws":
                return await self.websocket(request)
            if path == "/extensions":
                return await self.extensions(self.cpu.url)
            if path.startswith("/object_info"):
                if not self.journal.data["candidate"]:
                    await self.volumes["models"].reload.aio()
                return await self.objects(path)
            if path.startswith("/models") and not self.journal.data["candidate"]:
                await self.volumes["models"].reload.aio()
            if path.startswith("/extensions/"):
                response = await self.extension_file(path)
                if response is not None:
                    return response
            if path == "/prompt" and request.method == "POST":
                body = await request.json()
                async with self.lock:
                    job = self.journal.enqueue(body, request.headers.get("Idempotency-Key"))
                    await self.volumes["input"].commit.aio()
                    await self.persist()
                await self.status()
                return web.json_response({"prompt_id": job["id"], "number": job["number"], "node_errors": {}})
            if (path == "/jobs" or path.startswith("/jobs/")) and request.method == "GET":
                async with self.client.post(self.cpu.url + "/_split/jobs", json={
                    "queue": self.journal.queue(), "history": self.journal.history(),
                    "query": dict(request.query),
                    "job_id": path.split("/")[2] if path.startswith("/jobs/") else None,
                }) as response:
                    return web.Response(body=await response.read(), status=response.status,
                                        content_type="application/json")
            if path in ("/queue", "/prompt") and request.method == "GET":
                queue = self.journal.queue()
                return web.json_response(queue if path == "/queue" else {
                    "exec_info": {"queue_remaining": sum(len(v) for v in queue.values())}})
            if path == "/queue" and request.method == "POST":
                body = await request.json()
                async with self.lock:
                    ids = [j["id"] for j in self.journal.data["jobs"].values() if j["status"] == "queued"] if body.get("clear") else body.get("delete", [])
                    await self.cancel(ids)
                return web.json_response({})
            if path == "/interrupt" and request.method == "POST":
                body = await request.json() if request.can_read_body else {}
                async with self.lock:
                    await self.cancel([body["prompt_id"]] if body.get("prompt_id") else [
                        j["id"] for j in self.journal.data["jobs"].values() if j["status"] == "running"])
                return web.json_response({})
            if path.startswith("/jobs/") and path != "/jobs/cancel" and path.endswith("/cancel") and request.method == "POST":
                async with self.lock:
                    await self.cancel([path.split("/")[2]])
                return web.json_response({"cancelled": True})
            if path == "/jobs/cancel" and request.method == "POST":
                async with self.lock:
                    await self.cancel((await request.json()).get("job_ids", []))
                return web.json_response({"cancelled": True})
            if path.startswith("/history"):
                if request.method == "GET":
                    history = self.journal.history()
                    if path != "/history":
                        key = path.rsplit("/", 1)[-1]
                        history = {key: history[key]} if key in history else {}
                    return web.json_response(history)
                body = await request.json()
                async with self.lock:
                    for key, job in self.journal.data["jobs"].items():
                        if job["status"] not in ACTIVE and (body.get("clear") or key in body.get("delete", [])):
                            job["history"] = None
                    await self.persist()
                return web.json_response({})
            if path == "/free":
                # Never wake a GPU just to free its memory.
                return web.json_response({})
            if manager_path(path):
                return await self.manager(request, path)
            # CPU APIs cannot cause GPU activation. Unknown custom endpoints stay
            # local and can return unsupported, rather than triggering inference.
            async def commit_files():
                await asyncio.to_thread(self.cpu.archive_temp, legacy_paths=True)
                for name in ("input", "user", "output"):
                    await self.volumes[name].commit.aio()
            return await proxy(request, self.client, self.cpu.url,
                before_response=commit_files if request.method in {"POST", "PUT", "DELETE"} else None)
        except (ValueError, TypeError) as error:
            return web.json_response({"error": str(error)}, status=409)

    async def objects(self, path):
        async with self.client.get(self.cpu.url + "/object_info") as response:
            response.raise_for_status()
            objects = await response.json()
        catalog_path = environment_path(self.journal.data["environment"]) / "catalog.json"
        if catalog_path.exists():
            catalog = json.loads(catalog_path.read_text())
            # Live CPU definitions take precedence so model/file choices stay fresh.
            for name, sections in catalog.get("choice_sources", {}).items():
                if name in objects:
                    continue
                for section, fields in sections.items():
                    for field, folder in fields.items():
                        async with self.client.get(self.cpu.url + "/models/" + folder) as response:
                            if response.status == 200:
                                catalog["objects"][name]["input"][section][field][0] = await response.json()
            objects = dict(catalog["objects"], **objects)
        if path != "/object_info":
            name = path.rsplit("/", 1)[-1]
            objects = {name: objects[name]} if name in objects else {}
        return web.json_response(objects)

    async def extensions(self, origin, token=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        async with self.client.get(origin + "/extensions", headers=headers) as response:
            response.raise_for_status()
            items = await response.json()
        catalog_path = environment_path(self.journal.data["environment"]) / "catalog.json"
        if catalog_path.exists():
            for name, root in json.loads(catalog_path.read_text())["extensions"].items():
                directory = self.extension_root(root)
                if directory:
                    from urllib.parse import quote
                    items.extend("/extensions/" + quote(name, safe="") + "/" + p.relative_to(directory).as_posix()
                                 for p in directory.rglob("*.js"))
        return web.json_response(list(dict.fromkeys(items)))

    def extension_root(self, root):
        # Catalog GPU paths are resolved to the immutable environment, not trusted
        # as arbitrary files accessible to a browser.
        value = str(root)
        marker = "/custom_nodes/"
        if marker not in value:
            return None
        base = environment_path(self.journal.data["environment"]) / "comfy/custom_nodes"
        resolved = (base / value.split(marker, 1)[1]).resolve()
        return resolved if resolved.is_relative_to(base.resolve()) else None

    async def extension_file(self, path):
        parts = path.split("/", 3)
        catalog_path = environment_path(self.journal.data["environment"]) / "catalog.json"
        if len(parts) != 4 or not catalog_path.exists():
            return None
        root = json.loads(catalog_path.read_text())["extensions"].get(parts[2])
        directory = self.extension_root(root) if root else None
        if not directory:
            return None
        file = (directory / parts[3]).resolve()
        if not file.is_relative_to(directory) or not file.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(file)


def application(controller):
    app = web.Application(client_max_size=1024 ** 3)
    app.router.add_route("*", "/{path:.*}", controller.handle)
    app.on_startup.append(controller.start)
    app.on_cleanup.append(controller.close)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    names = json.loads(os.environ["SPLIT_VOLUMES"])
    volumes = {key: modal.Volume.from_name(name) for key, name in names.items()}
    app_name = os.environ["SPLIT_APP"]
    controller = Controller(modal.Function.from_name(app_name, "gpu_worker"),
        modal.Queue.from_name(app_name + "-events", create_if_missing=True),
        modal.Queue.from_name(app_name + "-commands", create_if_missing=True), volumes)
    web.run_app(application(controller), host="0.0.0.0", port=8000)
