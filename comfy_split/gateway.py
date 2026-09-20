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
from comfy_split import storage
from comfy_split import ambient_nodes
from comfy_split.ambient_workflows import WorkflowRegistry, validate_template, pin_references
from comfy_split.agent_bridge import AgentBridge, PREFIX as BRIDGE_PREFIX, requires_bridge
from comfy_split.runtime import (
    ComfyProcess, create_environment, environment_path, initialize_environment,
)
from comfy_split.state import ACTIVE, Journal, job_history, write_json

log = logging.getLogger(__name__)


def api_path(path):
    return path[4:] if path.startswith("/api/") else path


def manager_path(path):
    path = path.removeprefix("/v2")
    return path.startswith(("/manager/", "/customnode/", "/snapshot/", "/comfyui_manager/"))


class Controller:
    def __init__(self, worker, events, commands, volumes, root=storage.STATE, *, ui_function=None,
                 warmed_cpu=None):
        self.worker, self.events, self.commands, self.volumes = worker, events, commands, volumes
        self.journal = Journal(root)
        self.lock = asyncio.Lock()
        self.cpu = warmed_cpu or ComfyProcess("cpu", 8187)
        self.candidate = ComfyProcess("candidate", 8190)
        self.sockets = {}
        self.task = None
        self.apply_task = None
        self.client = None
        self.candidate_relays = {}
        self.gpu_stats_lock = asyncio.Lock()
        self.gpu_stats = None
        self.gpu_stats_expiry = 0
        self.ui_function = ui_function
        self.cpu_pinned = None
        self.cpu_scaling_lock = asyncio.Lock()
        self.cleanup_due = 0.0
        self.bridge = AgentBridge(self)
        self.ambient_workflows = WorkflowRegistry(self.journal.data)
        self.ambient_templates_loaded = False
        self.starting = False
        self.startup_phase = "initializing"
        self.snapshot_status = None

    async def cleanup_storage(self):
        """Caller holds the controller lock; metadata reads never start a GPU."""
        if self.starting or self.background_work() or self.journal.data["mode"] != "split":
            return
        if time.monotonic() < self.cleanup_due:
            return
        self.cleanup_due = time.monotonic() + 5
        stats = await self.worker.get_current_stats.aio()
        if stats.num_total_runners or stats.backlog:
            return
        data = self.journal.data
        environment_volume = self.volumes["environment"]
        environments = [Path(entry.path).name async for entry in
                        environment_volume.iterdir.aio("/", recursive=False)]
        receipts = await storage.remote_receipts(self.volumes["data"])
        temporary = await storage.remote_files(self.volumes["output"], "/.split-temp")
        live = ["temp"] if self.cpu.process and self.cpu.process.returncode is None else []
        plan = storage.cleanup_plan(data, environments, receipts, temporary,
                                    live_temp_namespaces=live,
                                    snapshot_environments=await storage.snapshot_environments(environment_volume))
        # A durable tombstone precedes every receipt deletion, including old
        # validation/legacy sessions that no longer appear in the main journal.
        self.journal.retire(plan["expired_jobs"])
        result_statuses = {entry["path"]: entry["result"].get("status") for entry in receipts}
        for name in plan["receipts"]:
            job_id = name.removesuffix(".started.json") if name.endswith(".started.json") else name[:-5]
            data["retired_jobs"].setdefault(job_id, {"id": job_id,
                "status": result_statuses.get(job_id + ".json") or "failed"})
        await self.persist()
        for name in plan["receipts"]:
            with contextlib.suppress(FileNotFoundError):
                await self.volumes["data"].remove_file.aio("/jobs/" + name)
        for version in plan["environments"]:
            with contextlib.suppress(FileNotFoundError):
                await environment_volume.remove_file.aio("/" + version, recursive=True)
        if not data.get("core_copy_pruned") and not plan["unresolved_receipts"]:
            async for entry in environment_volume.iterdir.aio("/base/comfy", recursive=False):
                if Path(entry.path).name != "custom_nodes":
                    await environment_volume.remove_file.aio(entry.path, recursive=True)
            data["core_copy_pruned"] = True
            await self.persist()
        for relative in plan["temporary_files"]:
            with contextlib.suppress(FileNotFoundError):
                await self.volumes["output"].remove_file.aio("/.split-temp/" + relative)
        parents = {parent for relative in plan["temporary_files"] for parent in Path(relative).parents
                   if parent != Path(".")}
        for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
            path = "/.split-temp/" + parent.as_posix()
            with contextlib.suppress(FileNotFoundError):
                entries = [entry async for entry in self.volumes["output"].iterdir.aio(path, recursive=False)]
                if not entries:
                    await self.volumes["output"].remove_file.aio(path, recursive=True)
        self.cleanup_due = time.monotonic() + 300
        if any(plan[key] for key in ("expired_jobs", "receipts", "environments", "temporary_files")):
            log.info("Storage cleanup: %s", {key: len(plan[key]) for key in (
                "expired_jobs", "receipts", "environments", "temporary_files")})

    def background_work(self):
        data = self.journal.data
        return bool(data["session"] or any(
            job["status"] in {"queued", "dispatching", "running"}
            for job in data["jobs"].values()) or
            (data["candidate"] and data["candidate"]["status"] in {"creating", "validating"}))

    async def pin_cpu(self, needed):
        if self.ui_function is None:
            return
        async with self.cpu_scaling_lock:
            needed = needed or self.starting
            if self.cpu_pinned == needed:
                return
            await self.ui_function.update_autoscaler.aio(min_containers=int(needed))
            self.cpu_pinned = needed

    async def reconcile_cpu_scaling(self):
        if self.ui_function is None:
            return
        needed = self.background_work()
        candidate = self.journal.data["candidate"]
        if not needed and candidate and candidate["status"] == "editing":
            # Manager installs continue after their HTTP request returns. Keep
            # the CPU alive until its native queue finishes, then persist files.
            if self.candidate.process and self.candidate.process.returncode is None:
                async with self.client.get(self.candidate.url + "/v2/manager/queue/status",
                                           timeout=ClientTimeout(total=5)) as response:
                    response.raise_for_status()
                    state = await response.json()
                needed = bool(state["is_processing"] or state.get("pending_count", 0))
                if not needed and self.cpu_pinned:
                    await self.volumes["environment"].commit.aio()
        await self.pin_cpu(needed)

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
        # Acquire before acknowledging durable work; a closed browser must not
        # let the CPU disappear while dispatch/maintenance runs in the background.
        if self.background_work():
            await self.pin_cpu(True)
        self.journal.save()
        await self.volumes["data"].commit.aio()

    async def start(self, app):
        self.client = ClientSession(timeout=ClientTimeout(total=None), auto_decompress=False)
        await asyncio.to_thread(initialize_environment)
        await self.volumes["environment"].commit.aio()
        await self.volumes["data"].commit.aio()
        self.journal.recover()
        candidate = self.journal.data["candidate"]
        session = self.journal.data["session"]
        if candidate and candidate["status"] in {"creating", "validating"}:
            if not (session and session.get("operation") == "validate"):
                candidate.update(status="failed", error="環境更新中にCPUが再起動しました。旧環境を維持しています。")
        await self.persist()
        await self.refresh_ambient_nodes()
        self.startup_phase = "starting_comfy"
        warm_process = self.cpu.process
        if self.journal.data["mode"] == "split":
            await self.cpu.start(self.journal.data["environment"], cpu=True)
        else:
            await self.cpu.stop()
        if self.snapshot_status is not None:
            self.snapshot_status["reused"] = (warm_process is not None and
                self.cpu.process is warm_process and self.cpu.version is not None and
                self.cpu.version == self.snapshot_status.get("environment"))
        # Retention can remove large old environments. Let the UI and first
        # requests finish before the dispatcher performs normal idle cleanup.
        self.cleanup_due = time.monotonic() + 300
        self.task = asyncio.create_task(self.dispatch())
        if candidate and candidate["status"] == "validating" and session:
            self.apply_task = asyncio.create_task(self.apply_environment(resume=True))

    async def refresh_ambient_nodes(self):
        if not ambient_nodes.enabled():
            return
        data = self.journal.data
        if data["mode"] != "split" or self.journal.busy() or data["candidate"] or data["session"]:
            log.info("Ambient node refresh deferred until an idle CPU startup")
            return
        previous = data["environment"]
        deployment = os.environ.get(ambient_nodes.DEPLOYMENT_ENV, "unversioned")
        revisions = await asyncio.to_thread(ambient_nodes.snapshot_revisions, previous)
        last_refresh = data.get(ambient_nodes.REFRESH_KEY) or {}
        if (revisions and last_refresh.get("deployment") == deployment
                and last_refresh.get("revisions") == revisions):
            log.info("Ambient nodes reused without GitHub access: %s (last update: %s)",
                     previous, last_refresh.get("status"))
            return
        started = time.monotonic()
        self.startup_phase = "updating_nodes"
        await self.pin_cpu(True)
        try:
            try:
                version = await asyncio.to_thread(ambient_nodes.prepare_environment, previous)
                if version is not None:
                    # Validate imports without starting a GPU or running any node/API task.
                    self.startup_phase = "validating_nodes"
                    await self.candidate.start(version, cpu=True)
                    ambient_nodes.check_catalog(await self.candidate.catalog(self.client))
            except Exception:
                log.exception("Ambient node refresh failed; keeping environment %s", previous)
                if revisions:
                    # A valid previous snapshot remains usable. Do not delay every
                    # cold start with the same failed update; redeploy to retry.
                    data[ambient_nodes.REFRESH_KEY] = {"deployment": deployment,
                        "revisions": revisions, "status": "failed", "checked_at": time.time()}
                    await self.persist()
                return
            finally:
                await self.candidate.stop()
            # Commit the complete snapshot before any new job can reference it.
            if version is not None:
                await self.volumes["environment"].commit.aio()
                data["environment"] = version
            data[ambient_nodes.REFRESH_KEY] = {"deployment": deployment,
                "revisions": await asyncio.to_thread(ambient_nodes.snapshot_revisions,
                                                     data["environment"]),
                "status": "updated" if version else "unchanged", "checked_at": time.time()}
            await self.persist()
            log.info("Ambient nodes checked for deployment: %s -> %s (%.2fs)",
                     previous, data["environment"], time.monotonic() - started)
        finally:
            await self.pin_cpu(False)

    async def close(self, app):
        await self.bridge.close()
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
        # Mirror events are namespaced and correlated to a prompt, never injected
        # into unrelated browser sessions' native execution state.
        if client_id and isinstance(event, dict) and event.get("data", {}).get("prompt_id"):
            job = self.journal.data["jobs"].get(event["data"]["prompt_id"])
            meta = job and job.get("body", {}).get("extra_data", {}).get("ambient")
            if meta:
                job["ambient_event"] = event
                await self.broadcast({"type": "ambient_execution", "data": {
                    "sessionId": meta["sessionId"], "stage": meta["stage"], "event": event}})
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
        if record.get("agent_bridge"):
            try:
                if record["agent_bridge"] != self.bridge.identity():
                    raise ValueError("Mac Bridgeの接続が変わりました。再実行してください。")
            except ValueError as error:
                await self.finish(record, {"status": "failed", "error": str(error)})
                return
        # Persist intent BEFORE spawn. A crash between spawn and call-id commit
        # leaves an unknown job, which recovery must never blindly resubmit.
        record["status"] = "dispatching"
        await self.persist()
        spec = {"id": record["id"], "operation": operation,
                "environment": record["environment"]}
        for key in ("body", "token", "agent_bridge"):
            if key in record:
                spec[key] = record[key]
        call = await self.worker.spawn.aio(spec)
        record["call_id"] = call.object_id
        record["status"] = "running"
        await self.persist()

    async def read_result(self, record):
        try:
            return await storage.read_json(self.volumes["data"], "jobs/" + record["id"] + ".json")
        except FileNotFoundError:
            pass
        if not record.get("call_id"):
            return None
        call = modal.FunctionCall.from_id(record["call_id"])
        try:
            return await call.get.aio(timeout=0)
        except modal.exception.FunctionTimeoutError as error:
            # This is a terminal worker failure, unlike a poll with no result yet.
            return {"status": "failed", "error": str(error)}
        except (TimeoutError, modal.exception.TimeoutError):
            return None
        except Exception as error:
            # FunctionCall failure is terminal; a network failure is not.
            if isinstance(error, modal.exception.RemoteError) or any(
                    frame.filename.startswith("<ta-") for frame in traceback.extract_tb(error.__traceback__)):
                return {"status": "failed", "error": str(error)}
            raise

    async def drain_events(self, record, *, all_pending=False):
        while True:
            events = await self.events.get_many.aio(100, block=False, partition=record["id"])
            for event in events:
                await self.relay_event(record, event)
            if not all_pending or len(events) < 100:
                break

    async def relay_event(self, record, event):
        if event["type"] == "legacy_ready":
            record["url"] = event["url"]
            await self.persist()
        elif event["type"] == "agent_bridge_ready":
            try:
                await self.bridge.attach(record, event)
            except Exception:
                # Do not persist/log the tunnel bearer token from the event.
                log.warning("AgentRuntime Bridge connection failed for %s", record["id"])
                await self.command(record["id"], "agent_bridge_error")
            else:
                await self.command(record["id"], "agent_bridge_connected")
        elif event["type"] in {"event", "preview"}:
            data = event.get("event", event.get("data"))
            kind = data.get("type") if isinstance(data, dict) else None
            # Terminal notifications are reconstructed from durable history
            # in finish(), after output reload and journal persistence.
            if kind in {"execution_success", "execution_error", "execution_interrupted"}:
                return
            # Files referenced in executed events may not be committed yet.
            if kind == "executed":
                record.setdefault("deferred_events", []).append(data)
            else:
                await self.broadcast(data, record.get("body", {}).get("client_id"))
            # Frontend 1.52 counts `executed`/`execution_cached` for Total,
            # while core reports non-output node completion in progress_state.
            # Emit one empty UI result only for those genuinely finished
            # prompt nodes; leave real UI results and expanded child IDs alone.
            if kind in {"executed", "execution_cached", "progress_state"}:
                detail = data.get("data", {})
                notified = record.setdefault("completed_nodes", [])
                if kind == "executed":
                    notified.append(str(detail["node"]))
                elif kind == "execution_cached":
                    notified.extend(str(node) for node in detail.get("nodes", []))
                elif detail.get("prompt_id") == record["id"]:
                    prompt = record.get("body", {}).get("prompt", {})
                    for node_id, node in detail.get("nodes", {}).items():
                        if node.get("state") != "finished" or node_id not in prompt or node_id in notified:
                            continue
                        notified.append(node_id)
                        await self.broadcast({"type": "executed", "data": {
                            "prompt_id": record["id"], "node": node_id,
                            "display_node": node.get("display_node_id", node_id), "output": {},
                        }}, record.get("body", {}).get("client_id"))

    async def finish(self, record, result):
        # A result can arrive while more than one dispatch batch remains queued.
        await self.drain_events(record, all_pending=True)
        await self.bridge.detach(record["id"])
        await self.volumes["output"].reload.aio()
        record.update({key: value for key, value in result.items()
                       if key in {"status", "error", "history", "seconds"}})
        record["finished_at"] = time.time()
        record["history"] = job_history(record)
        deferred = record.pop("deferred_events", [])
        record.pop("completed_nodes", None)
        await self.persist()
        self.cleanup_due = 0
        delivered = set()
        for event in deferred:
            await self.broadcast(event, record.get("body", {}).get("client_id"))
            delivered.add(str(event["data"]["node"]))
        # Queue overflow/disconnection must not lose saved node previews.
        for node_id, output in record["history"]["outputs"].items():
            if node_id not in delivered:
                meta = record["history"].get("meta", {}).get(node_id, {})
                await self.broadcast({"type": "executed", "data": {
                    "prompt_id": record["id"], "node": node_id,
                    "display_node": meta.get("display_node", node_id), "output": output,
                }}, record.get("body", {}).get("client_id"))
        terminal = {"completed": "execution_success", "cancelled": "execution_interrupted"}.get(
            record["status"], "execution_error")
        messages = record["history"].get("status", {}).get("messages", [])
        detail = next((data for kind, data in reversed(messages) if kind == terminal),
                      {"prompt_id": record["id"], "timestamp": int(record["finished_at"] * 1000)})
        await self.broadcast({"type": terminal, "data": detail}, record.get("body", {}).get("client_id"))
        await self.broadcast({"type": "executing", "data": {"node": None, "prompt_id": record["id"]}})
        await self.status()

    async def dispatch(self):
        while True:
            try:
                async with self.lock:
                    await self.reconcile_cpu_scaling()
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
                    await self.cleanup_storage()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Dispatcher failed; preserving journal for recovery")
                await asyncio.sleep(2)

    async def end_legacy(self, result):
        await self.volumes["output"].reload.aio()
        await self.volumes["data"].reload.aio()
        for job_id, history in result.get("legacy_history", {}).items():
            if job_id not in self.journal.data["jobs"]:
                self.journal.data["jobs"][job_id] = {
                    "id": job_id, "number": history["prompt"][0],
                    "body": {"prompt": history["prompt"][2]}, "status": "completed",
                    "history": history, "created_at": time.time(), "finished_at": time.time(), "error": None}
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
            if not self.sockets[sid]:
                self.sockets.pop(sid, None)
        return socket

    async def cancel(self, ids):
        for job_id in ids:
            job = self.journal.data["jobs"].get(job_id)
            if not job:
                continue
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["finished_at"] = time.time()
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
                if self.bridge.socket is not None:
                    raise ValueError("Mac Bridgeを切断してからモードを切り替えてください。")
                self.journal.assert_idle()
                await self.volumes["input"].commit.aio()
                await self.volumes["data"].commit.aio()
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
            timeout_seconds = int(os.environ.get("SPLIT_VALIDATION_TIMEOUT", "3600"))
            deadline = time.time() + timeout_seconds
            while "result" not in session:
                if time.time() >= deadline:
                    await self.command(session["id"], "interrupt")
                    raise RuntimeError(f"GPU validation timed out after {timeout_seconds} seconds")
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
                self.cleanup_due = 0
            await self.broadcast({"type": "split_environment", "data": {"status": "ready"}})
        except Exception as error:
            log.exception("Environment validation failed")
            async with self.lock:
                candidate["status"] = "failed"
                candidate["error"] = str(error)
                # Clear session to allow discard to proceed. Send interrupt command
                # to stop any running GPU validation before clearing the session.
                session = self.journal.data["session"]
                if session and session.get("operation") == "validate":
                    if session.get("call_id"):
                        await self.command(session["id"], "interrupt")
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
                await self.pin_cpu(True)
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
            if path.startswith(BRIDGE_PREFIX + "/"):
                if not ambient_nodes.enabled():
                    raise web.HTTPNotFound()
                if self.journal.data["mode"] != "split" or self.journal.data["candidate"]:
                    raise ValueError("Bridgeは環境更新が完了した分離モードで接続してください。")
                return await self.bridge.handle(request)
            if path == "/view" and request.query.get("type") == "temp":
                # Previously persisted temp references remain readable after the
                # normal ComfyUI startup cleanup has been restored.
                root = (storage.TEMP_ARCHIVE / "temp").resolve()
                file = (root / request.query.get("subfolder", "") / request.query.get("filename", "")).resolve()
                if not file.is_relative_to(root):
                    raise web.HTTPForbidden()
                if file.is_file():
                    return web.FileResponse(file)
            if path in {"/split/status", "/modal-control/v1/status"}:
                data = self.journal.data
                return web.json_response({"api_version": 1, "mode": data["mode"], "environment": data["environment"],
                    "dependencies": getattr(self.cpu, "dependencies", {}),
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
                    self.cleanup_due = 0
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
                if request.headers.get("X-Modal-Execution-Mode") == "split":
                    return web.json_response({"error": "This client requires split mode."}, status=409)
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
                    # Mode may have changed while the request body was being read.
                    if self.journal.data["mode"] != "split":
                        raise ValueError("This client requires split mode.")
                    request_id = request.headers.get("Idempotency-Key")
                    existing = request_id and any(j.get("request_id") == request_id for j in (
                        *self.journal.data["jobs"].values(), *self.journal.data["retired_jobs"].values()))
                    bridge_id = self.bridge.identity() if requires_bridge(body) and not existing else None
                    body = self.ambient_workflows.prepare(body)
                    job = self.journal.enqueue(body, request_id)
                    if bridge_id and job["status"] == "queued":
                        job.setdefault("agent_bridge", bridge_id)
                    await self.volumes["input"].commit.aio()
                    await self.persist()
                await self.status()
                return web.json_response({"prompt_id": job["id"], "number": job["number"], "node_errors": {}})
            if path.startswith("/ambient/"):
                if path == "/ambient/workflows" and request.method == "GET":
                    if not self.ambient_templates_loaded:
                        from ambient.h3 import workflow
                        from ambient.contracts import DEFAULT_BACKENDS
                        from ambient.tagging import recipe
                        from comfy_split.ambient_workflows import h3_metadata
                        response = await self.objects("/object_info")
                        objects = json.loads(response.body)
                        async with self.lock:
                            req = {"requestId": "00000000-0000-4000-8000-000000000000",
                                   "sessionId": "00000000-0000-4000-8000-000000000000",
                                   "workflowRevision": 0, "prompt": "A quiet natural scene",
                                   "sound": "Soft ambient sounds", "seed": 42, "resolution": "preview"}
                            for mode in DEFAULT_BACKENDS:
                                try:
                                    graph = workflow({**req, "mode": mode}, object_info=objects)
                                    self.ambient_workflows.prepare({"prompt": graph, "extra_data": {
                                        "ambient": h3_metadata({**req, "mode": mode}, graph)}})
                                except ValueError:
                                    pass  # Missing models/nodes are reported by normal readiness checks.
                            if "JevInterpret" in objects:
                                self.ambient_workflows.prepare(recipe({}, req["sessionId"], 0))
                            templates = json.loads((Path(__file__).parent.parent / "ambient" / "bridge_templates.json").read_text())
                            for stage, template in templates.items():
                                if all(node["class_type"] in objects for node in template["graph"].values()):
                                    self.ambient_workflows.state["defaults"][stage] = template
                            await self.persist()
                            self.ambient_templates_loaded = True
                    return web.json_response(self.ambient_workflows.describe())
                if path.startswith("/ambient/workflows/") and request.method == "GET":
                    return web.json_response(self.ambient_workflows.describe(int(path.rsplit("/", 1)[-1])))
                if path in {"/ambient/workflows/apply", "/ambient/workflows/validate"} and request.method == "POST":
                    payload = await request.json()
                    response = await self.objects("/object_info")
                    objects = json.loads(response.body)
                    async with self.lock:
                        if path.endswith("/validate"):
                            validate_template(payload["template"], self.ambient_workflows.state["defaults"][payload["stage"]], objects)
                            return web.json_response({"valid": True})
                        if payload["expectedRevision"] != self.ambient_workflows.state["revision"]:
                            raise ValueError("Workflow changed on another device. Reload before applying.")
                        validate_template(payload["template"], self.ambient_workflows.state["defaults"][payload["stage"]], objects)
                        template = await asyncio.to_thread(pin_references, payload["template"], self.volumes["input"])
                        result = self.ambient_workflows.apply(payload["stage"], template, payload["expectedRevision"], objects)
                        await self.persist()
                    await self.broadcast({"type": "ambient_workflows", "data": {"revision": result["revision"]}})
                    return web.json_response(result)
                if path.startswith("/ambient/executions") and request.method == "GET":
                    records = []
                    for job in self.journal.data["jobs"].values():
                        meta = job.get("body", {}).get("extra_data", {}).get("ambient")
                        if not meta or (request.query.get("sessionId") and meta["sessionId"] != request.query["sessionId"]):
                            continue
                        if path != "/ambient/executions" and job["id"] != path.rsplit("/", 1)[-1]:
                            continue
                        records.append({"id": job["id"], "status": job["status"], "createdAt": job["created_at"],
                                        "graph": job["body"]["prompt"], "meta": meta,
                                        "workflow": meta.get("layout") or job["body"].get("extra_data", {}).get("extra_pnginfo", {}).get("workflow"),
                                        "event": job.get("ambient_event"), "outputs": (job.get("history") or {}).get("outputs", {})})
                    records.sort(key=lambda item: item["createdAt"], reverse=True)
                    return web.json_response({"executions": records[:100]})
                return web.json_response({"error": "Unknown Ambient route"}, status=404)
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
                for name in ("input", "data", "output"):
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
            # Ambient packs are CPU-importable and always use live definitions.
            # Old GPU catalogs must not resurrect removed or disabled node IDs.
            catalog["objects"] = {name: definition for name, definition in catalog["objects"].items()
                                  if not ambient_nodes.is_ambient_node(definition)}
            # Live CPU definitions take precedence so model/file choices stay fresh.
            for name, sections in catalog.get("choice_sources", {}).items():
                if name in objects or name not in catalog["objects"]:
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
    from comfy_split.startup import StartupGate

    startup = StartupGate(controller)
    app = web.Application(client_max_size=1024 ** 3)
    app.router.add_route("*", "/{path:.*}", startup.handle)
    app.on_startup.append(startup.start)
    app.on_cleanup.append(startup.close)
    return app


def make_controller(volumes=None, *, warmed_cpu=None):
    names = json.loads(os.environ["SPLIT_VOLUMES"])
    volumes = volumes or {key: modal.Volume.from_name(name) for key, name in names.items()}
    app_name = os.environ["SPLIT_APP"]
    return Controller(modal.Function.from_name(app_name, "gpu_worker"),
        modal.Queue.from_name(app_name + "-events", create_if_missing=True),
        modal.Queue.from_name(app_name + "-commands", create_if_missing=True), volumes,
        ui_function=modal.Function.from_name(app_name, "ui"), warmed_cpu=warmed_cpu)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(application(make_controller()), host="0.0.0.0", port=8000)
