import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientConnectionError, ClientResponseError, ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from comfy_split import gpu, remote_mcp
from comfy_split.config import Settings
from comfy_split.extension_sources import EXTENSIONS, INTEGRATIONS
from comfy_split.gateway import Controller, api_path
from comfy_split.state import Journal, job_history
from tests.split.support import remote_mock, volume_mocks

ADAPTER = next(iter(INTEGRATIONS.values()))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_versioned_control_api_preserves_old_clients(self):
        self.worker.get_current_stats = remote_mock(SimpleNamespace(num_total_runners=0))
        for path in ("/modal-control/v1/status", "/split/status"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            state = await response.json()
            self.assertEqual(state["api_version"], 1)
            self.assertEqual(state["gpu"]["containers"], 0)
        response = await self.client.get("/extensions")
        self.assertNotIn("/split.js", await response.json())
        self.worker.spawn.aio.assert_not_awaited()

    async def test_gpu_display_reads_metadata_without_invoking_worker(self):
        self.worker.get_current_stats = remote_mock(SimpleNamespace(num_total_runners=0))
        states = await asyncio.gather(*[self.control.gpu_status() for _ in range(10)])
        self.assertTrue(all(s["phase"] == "stopped" and s["containers"] == 0 for s in states))
        self.worker.get_current_stats.aio.assert_awaited_once()
        self.worker.spawn.aio.assert_not_awaited()
        self.control.gpu_stats_expiry = 0
        self.worker.get_current_stats.aio.return_value = SimpleNamespace(num_total_runners=1)
        self.assertEqual((await self.control.gpu_status())["phase"], "stopping")
        self.control.journal.data["mode"] = "legacy"
        self.assertEqual((await self.control.gpu_status())["phase"], "legacy")
        self.control.gpu_stats_expiry = 0
        self.worker.get_current_stats.aio.side_effect = RuntimeError("unavailable")
        with self.assertLogs("comfy_split.gateway", level="WARNING"):
            failed = await self.control.gpu_status()
        self.assertEqual(failed["phase"], "unknown")
        self.assertIsNone(failed["containers"])
        self.assertIsNone(failed["checked_at"])

    async def test_definition_failure_preserves_manual_gpu_and_auto_is_retryable(self):
        body = {"prompt": {"1": {"class_type": "Test", "inputs": {}}}}
        for error in (ClientConnectionError("restarting"),
                      ClientResponseError(None, (), status=503), asyncio.TimeoutError()):
            with patch.object(self.control, "objects", AsyncMock(side_effect=error)):
                response = await self.client.post("/api/prompt", json={**body, "gpu": "L40S"})
                self.assertEqual(response.status, 200)
                receipt = await response.json()
                self.assertEqual(receipt["gpu"], "L40S")
                self.assertIn("ノード定義・入力を確認できません", receipt["gpu_reason"])
                count = len(self.control.journal.data["jobs"])
                response = await self.client.post("/api/prompt", json=body)
                self.assertEqual(response.status, 503)
                self.assertEqual(len(self.control.journal.data["jobs"]), count)

    async def test_cpu_job_skips_gpu_wait_and_is_not_counted_as_gpu_work(self):
        cpu = SimpleNamespace(spawn=remote_mock(SimpleNamespace(object_id="fc-cpu")),
                              get_current_stats=remote_mock(SimpleNamespace(num_total_runners=1, backlog=0)))
        self.control.cpu_worker = cpu
        self.worker.get_current_stats.aio.return_value = SimpleNamespace(num_total_runners=1, backlog=0)
        job = self.control.journal.enqueue({"prompt": {"1": {"class_type": "Test", "inputs": {}}}})
        job.update(gpu="CPU", gpu_reason="test")
        await self.control.spawn(job, "generate")
        cpu.spawn.aio.assert_awaited_once()
        self.worker.spawn.aio.assert_not_awaited()
        self.worker.get_current_stats.aio.assert_not_awaited()
        self.assertEqual(job["call_id"], "fc-cpu")
        self.assertEqual((await self.control.gpu_status())["phase"], "stopping")
        self.worker.get_current_stats.aio.return_value = SimpleNamespace(num_total_runners=0, backlog=0)
        self.control.gpu_stats_expiry = 0
        status = await self.control.gpu_status()
        self.assertEqual(status["containers"], 0)
        self.assertEqual(status["phase"], "stopped")
        self.assertEqual(status["latest_selection"]["gpu"], "CPU")
        cpu.get_current_stats.aio.assert_not_awaited()

    async def asyncSetUp(self):
        self.enterContext(patch.dict("os.environ", Settings().environment()))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.worker = SimpleNamespace(spawn=remote_mock(SimpleNamespace(object_id="fc-test")),
            get_current_stats=remote_mock(SimpleNamespace(num_total_runners=0, backlog=0)))
        self.events = SimpleNamespace(get_many=remote_mock([]))
        self.commands = SimpleNamespace(put=remote_mock())
        self.volumes = volume_mocks()
        async def missing_file(_):
            raise FileNotFoundError
            yield b""
        self.volumes["data"].read_file = SimpleNamespace(aio=missing_file)
        self.control = Controller(self.worker, self.events, self.commands, self.volumes,
                                  Path(self.temp.name))

        async def cpu_handler(request):
            if request.path == "/ws":
                socket = web.WebSocketResponse()
                await socket.prepare(request)
                await socket.send_json({"type": "status", "data": {"sid": "upstream"}})
                async for message in socket:
                    await socket.send_str(message.data)
                return socket
            if request.path.startswith("/object_info"):
                return web.json_response({"Test": {"input": {}}})
            if request.path == "/extensions":
                return web.json_response([])
            if request.path == "/_split/jobs":
                self.job_snapshot = await request.json()
                return web.json_response({"jobs": [], "pagination": {"total": 0}})
            reserved = [route for source in EXTENSIONS.values() for route in source["guarded_routes"]]
            # Native ComfyUI has no extension APIs. Adapter routes, however,
            # must be blocked by the gateway even if the upstream returns 200.
            if request.path in reserved:
                raise web.HTTPNotFound()
            return web.json_response({"cpu": True})

        cpu_app = web.Application()
        cpu_app.router.add_route("*", "/{path:.*}", cpu_handler)
        self.cpu_server = TestServer(cpu_app)
        self.addAsyncCleanup(self.cpu_server.close)
        await self.cpu_server.start_server()
        self.control.cpu = SimpleNamespace(url=str(self.cpu_server.make_url("/"))[:-1],
                                           stop=AsyncMock(), start=AsyncMock(), archive_temp=Mock())
        self.control.client = ClientSession(auto_decompress=False)
        self.addAsyncCleanup(self.control.client.close)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.control.handle)
        self.client = TestClient(TestServer(app))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()

    async def test_jobs_use_extension_snapshot_without_modifying_comfy_queue(self):
        job = self.control.journal.enqueue({"prompt": {"1": {"class_type": "Test"}}})
        response = await self.client.get("/api/jobs?limit=5&status=pending")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.job_snapshot["queue"]["queue_pending"][0][1], job["id"])
        self.assertEqual(self.job_snapshot["query"], {"limit": "5", "status": "pending"})
        self.worker.spawn.aio.assert_not_awaited()
        response = await self.client.post("/_split/jobs", json={})
        self.assertEqual(response.status, 404)

    async def test_image_browsing_repair_rejects_existing_candidate(self):
        self.control.journal.data["candidate"] = {"version": "env-existing", "status": "editing"}
        response = await self.client.post("/split/environment/repair-image-browsing")
        self.assertEqual(response.status, 409)
        self.assertEqual(self.control.journal.data["candidate"]["version"], "env-existing")

    async def test_browser_candidate_failure_is_saved_and_can_be_discarded(self):
        """Browser failures must leave a discardable state and preserve the active version."""
        c = self.control
        for stage in ("copy", "startup"):
            with self.subTest(stage=stage):
                with patch("comfy_split.gateway.create_environment", return_value="env-copy",
                        side_effect=OSError("copy failed") if stage == "copy" else None), \
                        patch.object(c.candidate, "start", AsyncMock(side_effect=RuntimeError("startup failed"))):
                    async with c.lock:
                        with self.assertRaisesRegex((OSError, RuntimeError), "failed"):
                            await c.ensure_candidate()
                self.assertEqual(Journal(Path(self.temp.name)).data["candidate"], c.journal.data["candidate"])
                if stage == "copy":
                    self.assertIsNone(c.journal.data["candidate"])
                else:
                    self.assertEqual(c.journal.data["candidate"], {"version": "env-copy", "status": "failed"})
                    response = await self.client.post("/split/environment/discard")
                    self.assertEqual(response.status, 200)
                    self.assertIsNone(c.journal.data["candidate"])
                self.assertEqual(c.journal.data["environment"], "base")
                self.assertFalse(c.background_work())

    async def test_browser_cannot_discard_or_apply_candidate_while_mcp_prepares_it(self):
        """Browser actions must not replace the candidate owned by an ongoing MCP preparation."""
        self.control.journal.data["candidate"] = {"version": None, "status": "creating"}
        self.control.prepare_task = asyncio.create_task(asyncio.Event().wait())
        try:
            for path in ("/split/environment/discard", "/manager/reboot"):
                response = await self.client.post(path)
                self.assertEqual(response.status, 409)
                self.assertEqual(self.control.journal.data["candidate"]["status"], "creating")
                self.assertIsNone(self.control.apply_task)
        finally:
            self.control.prepare_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await self.control.prepare_task

    async def test_image_browsing_repair_rejects_queued_generation(self):
        self.control.journal.enqueue({"prompt": {"1": {}}})
        response = await self.client.post("/api/split/environment/repair-image-browsing")
        self.assertEqual(response.status, 409)
        self.assertIsNone(self.control.journal.data["candidate"])

    async def test_internal_mcp_requires_both_headers_and_split_mode(self):
        headers = {"X-Modal-Execution-Mode": "split", "X-Split-Remote-Tool": "1"}
        with patch.object(remote_mcp, "run_tool", new_callable=AsyncMock) as run:
            for mode in ("split", "legacy"):
                self.control.journal.data["mode"] = mode
                for missing in headers:
                    response = await self.client.post("/_remote_mcp/call", json={"arguments": {}},
                        headers={key: value for key, value in headers.items() if key != missing})
                    self.assertEqual(response.status, 403)
            response = await self.client.post("/_remote_mcp/call", json={"arguments": {}}, headers=headers)
            self.assertEqual(response.status, 409)
            run.assert_not_awaited()
        self.worker.spawn.aio.assert_not_awaited()

    async def test_read_progress_reports_native_steps_timing_and_errors_only_for_one_job(self):
        headers = {"X-Modal-Execution-Mode": "split", "X-Split-Remote-Tool": "1"}
        job, other = [self.control.journal.enqueue({"prompt":{"1":{}}}) for _ in range(2)]
        async def snapshot():
            response = await self.client.post("/_remote_mcp/progress", json={"prompt_id": job["id"]}, headers=headers)
            self.assertEqual(response.status, 200)
            return await response.json()
        self.assertIsNone((await snapshot())["progress"])
        job.update(status="running", created_at=100)
        async def event(kind, data):
            await self.control.relay_event(job, {"type":"event", "event":{"type":kind,"data":data}})
        await event("executing", {"node":"2", "prompt_id":job["id"]})
        await event("progress", {"node":"2", "value":3, "max":8, "prompt_id":job["id"]})
        await event("progress", {"node":"4", "value":4, "max":8, "prompt_id":other["id"]})
        self.assertEqual((await snapshot())["progress"], {"node":"2", "value":3, "max":8})
        await event("executing", {"node":"3", "prompt_id":job["id"]})
        self.assertEqual((await snapshot())["progress"], {"node":"3"})
        job.update(status="failed", seconds=6, finished_at=112, history={"status":{"messages":[
            ["execution_start",{"timestamp":2000}], ["execution_error",{"timestamp":4500,"node_id":"3",
             "exception_message":"missing input","current_inputs":{"private":"redacted"}}]]}})
        result = await snapshot()
        self.assertEqual((result["execution_seconds"],result["elapsed_seconds"],result["worker_seconds"]),(2.5,12,6))
        self.assertEqual(result["error"], {"node_id":"3", "exception_message":"missing input"})
        self.assertIsNone(result["progress"])
        unauthorized = await self.client.post("/_remote_mcp/progress",json={"prompt_id":job["id"]})
        self.assertEqual(unauthorized.status,403)
        self.worker.spawn.aio.assert_not_awaited()

    async def test_remote_cancel_is_scoped_idempotent_and_preserves_terminal_outputs(self):
        headers = {"X-Modal-Execution-Mode": "split", "X-Split-Remote-Tool": "1"}
        jobs = [self.control.journal.enqueue({"prompt":{"1":{}}}) for _ in range(4)]
        queued, running, finished, other = jobs
        running["status"] = "running"
        finished.update(status="completed", history={"outputs":{"file":"keep"}})
        async def cancel(job_id):
            response = await self.client.post("/_remote_mcp/cancel",json={"prompt_id":job_id},headers=headers)
            return response.status, (await response.json())["status"]
        for _ in range(2):
            self.assertEqual(await cancel(queued["id"]), (200, "cancelled"))
            self.assertEqual(await cancel(running["id"]), (200, "cancel_requested"))
        self.commands.put.aio.assert_awaited_once_with({"type":"interrupt"}, partition=running["id"])
        self.assertEqual(await cancel(finished["id"]), (200,"completed"))
        history = await (await self.client.get("/history/"+queued["id"])).json()
        self.assertEqual(history[queued["id"]]["status"]["messages"][0][0], "execution_interrupted")
        self.assertEqual(finished["history"]["outputs"], {"file":"keep"})
        self.control.journal.retire([finished["id"]])
        for _ in range(2):
            self.assertEqual(await cancel(finished["id"]), (200, "completed"))
        self.commands.put.aio.assert_awaited_once()
        self.assertEqual(await cancel("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), (404,"not_found"))
        self.assertEqual(other["status"], "queued")
        self.worker.spawn.aio.assert_not_awaited()

    async def test_internal_mcp_bad_subprocess_response_is_a_gateway_failure(self):
        for script in ("raise SystemExit(7)", "print('invalid json')"):
            with patch.object(remote_mcp, "BRIDGE_COMMAND", (sys.executable, "-c", script)):
                response = await self.client.post("/_remote_mcp/call", json={"arguments": {}},
                    headers={"X-Modal-Execution-Mode": "split", "X-Split-Remote-Tool": "1"})
                self.assertEqual(response.status, 502)

    async def test_internal_mcp_timeout_kills_the_child_process_group(self):
        marker = Path(self.temp.name) / "child-group"
        child = (f"import os,time; open({str(marker)!r},'w').write(str(os.getpgrp())); "
                 "time.sleep(5)")
        script = (f"import subprocess,sys; subprocess.run([sys.executable,'-c',{child!r}])")
        with patch.object(remote_mcp, "BRIDGE_COMMAND", (sys.executable, "-c", script)), \
             patch.object(remote_mcp, "CALL_TIMEOUT", 1.5), \
             patch.object(remote_mcp.os, "killpg", wraps=os.killpg) as kill:
            response = await self.client.post("/_remote_mcp/call", json={"arguments": {}},
                headers={"X-Modal-Execution-Mode": "split", "X-Split-Remote-Tool": "1"})
            self.assertEqual(response.status, 504)
            kill.assert_called_once_with(int(marker.read_text()), signal.SIGKILL)

    async def test_edit_upload_view_and_websocket_never_spawn_gpu(self):
        for path in ("/", "/api/object_info", "/models", "/view?filename=a.png", "/extensions"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
        response = await self.client.post("/api/upload/image", data=b"input")
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/userdata/workflows%2Ftest.json", json={})
        self.assertEqual(response.status, 200)
        async with self.client.ws_connect("/ws?clientId=browser") as socket:
            message = await socket.receive_json()
            self.assertEqual(message["data"]["sid"], "browser")
        self.worker.spawn.aio.assert_not_awaited()
        self.volumes["input"].commit.aio.assert_awaited()

    async def test_parallel_tabs_serialize_durable_acceptance(self):
        responses = await asyncio.gather(*[
            self.client.post("/prompt", json={"prompt": {"1": {"class_type": "Test"}}})
            for _ in range(3)])
        ids = {(await response.json())["prompt_id"] for response in responses}
        self.assertEqual(len(ids), 3)
        disk = Journal(Path(self.temp.name))
        self.assertEqual(set(disk.data["jobs"]), ids)
        self.assertEqual(self.volumes["data"].commit.aio.await_count, 3)
        self.worker.spawn.aio.assert_not_awaited()

    async def test_manager_import_diagnostics_do_not_stage_an_environment(self):
        for path in ("/v2/customnode/import_fail_info", "/v2/customnode/import_fail_info_bulk"):
            response = await self.client.post(path, json={"urls": []})
            self.assertEqual(response.status, 200)
        self.assertIsNone(self.control.journal.data["candidate"])
        self.worker.spawn.aio.assert_not_awaited()

    async def test_cancel_pending_batch_does_not_wake_gpu(self):
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        response = await self.client.post("/api/jobs/cancel", json={"job_ids": [job["id"]]})
        self.assertEqual(response.status, 200)
        self.assertEqual(job["status"], "cancelled")
        self.worker.spawn.aio.assert_not_awaited()
        self.commands.put.aio.assert_not_awaited()

    async def test_remote_python_failure_is_terminal_but_network_failure_is_not(self):
        namespace = {}
        # A fixed fixture with Modal's remote traceback filename, not user input.
        exec(compile("def fail():\n raise RuntimeError('remote failure')", "<ta-test>:/root/worker.py", "exec"), namespace)  # noqa: S102
        try:
            namespace["fail"]()
        except RuntimeError as error:
            remote_error = error
        call = SimpleNamespace(get=remote_mock())
        with patch("comfy_split.gateway.modal.FunctionCall.from_id", return_value=call):
            call.get.aio.side_effect = remote_error
            result = await self.control.read_result({"id": "missing-test-result", "call_id": "fc-test"})
            self.assertEqual(result["status"], "failed")
            call.get.aio.side_effect = ConnectionError("network unavailable")
            with self.assertRaises(ConnectionError):
                await self.control.read_result({"id": "missing-test-result", "call_id": "fc-test"})

    async def test_dispatch_fills_three_slots_and_refills_out_of_order(self):
        cpu = SimpleNamespace(spawn=remote_mock(SimpleNamespace(object_id="fc-cpu")))
        self.control.cpu_worker = cpu
        jobs = []
        for device in ("CPU", "CPU", self.control.session_gpu, self.control.session_gpu, "L4"):
            job = self.control.journal.enqueue({"prompt": {"1": {}}})
            job.update(gpu=device, gpu_reason="test")
            jobs.append(job)
        self.control.workers["L4"] = self.worker
        self.control.read_result = AsyncMock(return_value=None)
        await self.control.dispatch_jobs()
        self.assertEqual([j["status"] for j in jobs], ["running", "queued", "running", "running", "queued"])
        self.control.read_result.side_effect = lambda j: {"status": "completed"} if j is jobs[3] else None
        await self.control.dispatch_jobs()
        self.assertEqual([j["status"] for j in jobs], ["running", "queued", "running", "completed", "running"])
        self.assertEqual(cpu.spawn.aio.await_count, 1)
        self.assertEqual(self.worker.spawn.aio.await_count, 3)
        jobs[2]["status"] = "unknown"
        self.control.read_result.side_effect = lambda j: {"status": "completed"} if j is jobs[0] else None
        await self.control.dispatch_jobs()
        self.assertEqual([j["status"] for j in jobs], ["completed", "queued", "unknown", "completed", "running"])

    async def test_allocation_timeout_and_manual_switch_only_grant_current_attempt(self):
        self.control.workers.update(L4=self.worker, L40S=self.worker)
        self.control.read_result = AsyncMock(return_value=None)
        call = SimpleNamespace(cancel=remote_mock())
        self.enterContext(patch("modal.FunctionCall.from_id", return_value=call))
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        job.update(gpu="L4", gpu_reason="test")
        with patch("time.time", return_value=1000):
            await self.control.dispatch_jobs()
        initial = job["allocation"]
        with patch("time.time", return_value=1179):
            await self.control.dispatch_jobs()
        self.assertEqual(job["gpu"], "L4")
        with patch("time.time", return_value=1180):
            await self.control.dispatch_jobs()
        self.assertEqual(job["gpu"], "RTX-PRO-6000")
        self.assertNotEqual(job["allocation"], initial)
        await self.control.relay_event(job, {"type": "allocation_ready", "allocation": initial})
        self.commands.put.aio.assert_awaited_with("stop", partition=initial, partition_ttl=240)
        url = "/split/jobs/" + job["id"] + "/gpu"
        self.assertEqual((await self.client.post(url, json={"gpu": "L40S"})).status, 200)
        await self.control.dispatch_jobs()
        token = job["allocation"]
        job["submitted_at"] -= 300
        await self.control.dispatch_jobs()
        self.assertEqual(job["gpu"], "L40S")
        await self.control.relay_event(job, {"type": "allocation_ready", "allocation": token})
        self.commands.put.aio.assert_awaited_with("run", partition=token, partition_ttl=240)
        self.assertTrue(Journal(Path(self.temp.name)).data["jobs"][job["id"]]["allocation_granted"])
        self.assertEqual((await self.client.post(url, json={"gpu": "L4"})).status, 409)
        self.assertEqual(call.cancel.aio.await_count, 2)

    async def test_fallback_settings_and_cancelling_allocation(self):
        settings = {"enabled": False, "minutes": 1, "gpu": "L40S"}
        url = "/split/gpu-fallback"
        self.assertEqual((await self.client.post(url, json={**settings, "minutes": 0})).status, 400)
        self.assertEqual((await self.client.post(url, json=settings)).status, 200)
        self.assertEqual(Journal(Path(self.temp.name)).data["gpu_fallback"], settings)
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        job.update(gpu=self.control.session_gpu, gpu_reason="test")
        self.control.read_result = AsyncMock(return_value=None)
        await self.control.dispatch_jobs()
        job["submitted_at"] -= 300
        await self.control.dispatch_jobs()
        state = await (await self.client.get("/split/status")).json()
        self.assertEqual([j["id"] for j in state["waiting_for_gpu"]], [job["id"]])
        self.assertEqual(job["gpu"], self.control.session_gpu)
        with patch("modal.FunctionCall.from_id", return_value=SimpleNamespace(cancel=remote_mock())):
            await self.control.cancel([job["id"]])
        self.assertEqual(job["status"], "cancelled")
        self.assertNotIn("allocation", job)
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        job["mcp_guard"] = {"routes": [["approved"]]}
        await self.control.dispatch_jobs()
        self.assertNotIn("allocation", job)

    async def test_interrupt_and_unlabelled_previews_are_scoped_during_parallel_runs(self):
        jobs = [self.control.journal.enqueue({"prompt": {"1": {}}, "client_id": sid})
                for sid in ("same", "same", "other")]
        for job in jobs:
            job["status"] = "running"
        response = await self.client.post("/api/interrupt", json={})
        self.assertEqual(response.status, 409)
        self.commands.put.aio.assert_not_awaited()
        self.assertEqual((await self.client.post("/api/interrupt", json={"prompt_id": jobs[1]["id"]})).status, 200)
        self.commands.put.aio.assert_awaited_once_with({"type": "interrupt"}, partition=jobs[1]["id"])
        self.control.broadcast = AsyncMock()
        for job in jobs:
            await self.control.relay_event(job, {"type": "preview", "data": b"preview"})
        self.control.broadcast.assert_awaited_once_with(b"preview", "other")

    async def test_browser_receives_its_preview_during_an_mcp_job(self):
        browser = self.control.journal.enqueue({"prompt": {"1": {}}, "client_id": "browser"})
        mcp = self.control.journal.enqueue({"prompt": {"1": {}}})
        browser["status"] = mcp["status"] = "running"
        async with self.client.ws_connect("/ws?clientId=browser") as socket:
            await socket.receive_json()  # Initial queue status.
            await self.control.relay_event(mcp, {"type": "preview", "data": b"mcp preview"})
            await self.control.relay_event(browser, {"type": "preview", "data": b"browser preview"})
            await self.control.broadcast(b"end", "browser")
            self.assertEqual(await socket.receive_bytes(timeout=2), b"browser preview")
            self.assertEqual(await socket.receive_bytes(timeout=2), b"end")

    async def test_preview_is_visible_while_a_same_client_job_cannot_generate(self):
        body = {"prompt": {"1": {}}, "client_id": "browser"}
        sender = self.control.journal.enqueue(body)
        peer = self.control.journal.enqueue(body)
        sender["status"] = "running"
        peer.update(allocation="attempt", allocation_granted=False)
        self.control.broadcast = AsyncMock()
        for status in ("running", "unknown"):
            with self.subTest(status=status):
                peer["status"] = status
                self.control.broadcast.reset_mock()
                await self.control.relay_event(sender, {"type": "preview", "data": b"preview"})
                self.control.broadcast.assert_awaited_once_with(b"preview", "browser")

    async def test_spawn_failure_keeps_dispatch_intent_and_prevents_second_job(self):
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        self.worker.spawn.aio.side_effect = RuntimeError("connection lost after send")
        with self.assertRaises(RuntimeError):
            await self.control.spawn(job, "generate")
        disk = Journal(Path(self.temp.name))
        disk.recover()
        self.assertEqual(disk.data["jobs"][job["id"]]["status"], "unknown")
        self.assertIsNone(disk.next_job())

    async def test_session_gpu_alias_counts_named_pool_but_dispatches_to_session_worker(self):
        for device in ("L4", "L40S"):
            pools = {}
            def lookup(app, name):
                pools[name] = SimpleNamespace(spawn=remote_mock(SimpleNamespace(object_id="fc-test")),
                    get_current_stats=remote_mock(SimpleNamespace(num_total_runners=0, backlog=0)))
                return pools[name]
            with patch("modal.Function.from_name", side_effect=lookup):
                self.control.workers = gpu.deployed_workers("test", device)
            named_pool = pools[gpu.WORKERS[device]]
            named_pool.get_current_stats.aio.return_value = SimpleNamespace(num_total_runners=1, backlog=0)
            self.control.gpu_stats_expiry = 0
            self.assertEqual((await self.control.gpu_status())["containers"], 1)
            job = self.control.journal.enqueue({"prompt": {"1": {}}})
            job.update(gpu=device, gpu_reason="manual")
            await self.control.spawn(job, "generate")
            pools["gpu_worker"].spawn.aio.assert_awaited_once()


    async def test_progress_counts_finished_non_output_nodes_and_preserves_real_outputs(self):
        job = self.control.journal.enqueue({"prompt": {str(n): {} for n in range(1, 5)}})
        self.control.broadcast = AsyncMock()
        async def relay(kind, data):
            await self.control.relay_event(job, {"type": "event", "event": {"type": kind, "data": data}})
        await relay("execution_cached", {"prompt_id": job["id"], "nodes": ["1"]})
        output = {"images": [{"filename": "real.png", "type": "output", "subfolder": ""}]}
        await relay("executed", {"prompt_id": job["id"], "node": "4", "output": output})
        states = {"1": {"state": "finished"}, "2": {"state": "finished"},
                  "3": {"state": "running", "value": 1, "max": 4},
                  "4": {"state": "finished"}, "expanded-child": {"state": "finished"}}
        for _ in range(2):
            await relay("progress_state", {"prompt_id": job["id"], "nodes": states})
        await relay("progress_state", {"prompt_id": "another-job", "nodes": {"3": {"state": "finished"}}})
        emitted = [call.args[0] for call in self.control.broadcast.call_args_list]
        completed = [event["data"] for event in emitted if event["type"] == "executed"]
        self.assertEqual(completed, [{"prompt_id": job["id"], "node": "2", "display_node": "2", "output": {}}])
        # The native frontend now counts cached node 1 + completed node 2 (50%).
        self.assertEqual(len({"1"} | {event["node"] for event in completed}) / 4, 0.5)
        self.assertEqual(job["deferred_events"][0]["data"]["output"], output)

    async def test_terminal_event_waits_for_files_and_readable_history_even_with_backlog(self):
        job = self.control.journal.enqueue({"prompt": {"1": {}}, "client_id": "browser"})
        job["status"] = "running"
        history = {"outputs": {"1": {"images": [{"filename": "saved.png"}]}},
                   "status": {"status_str": "success", "messages": [
                       ["execution_success", {"prompt_id": job["id"], "timestamp": 123}]]}}
        success = {"type": "event", "event": {"type": "execution_success", "data": {"prompt_id": job["id"]}}}
        progress = {"type": "event", "event": {"type": "progress", "data": {"value": 1, "max": 2}}}
        self.events.get_many.aio.side_effect = [[progress] * 100, [success]]
        self.control.broadcast = AsyncMock()
        ready = asyncio.Event()
        self.volumes["output"].reload.aio.side_effect = ready.wait
        task = asyncio.create_task(self.control.finish(job, {"status": "completed", "history": history}))
        await asyncio.sleep(0)
        self.assertEqual(job["status"], "running")
        self.assertTrue(all(call.args[0]["type"] == "progress" for call in self.control.broadcast.call_args_list))
        async def visible(event, client_id=None):
            self.assertEqual(Journal(Path(self.temp.name)).history()[job["id"]]["outputs"], history["outputs"])
        self.control.broadcast.side_effect = visible
        ready.set()
        await task
        self.assertEqual(job["status"], "completed")
        self.assertEqual(Journal(Path(self.temp.name)).history()[job["id"]], job_history(job))
        kinds = [call.args[0]["type"] for call in self.control.broadcast.call_args_list]
        self.assertEqual(kinds[-4:], ["executed", "execution_success", "executing", "status"])
        self.assertEqual(kinds.count("execution_success"), 1)
        self.assertEqual(self.events.get_many.aio.await_count, 2)

    async def test_worker_failure_has_valid_history_and_error_notification(self):
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        self.control.broadcast = AsyncMock()
        await self.control.finish(job, {"status": "failed", "error": "worker stopped"})
        event = self.control.broadcast.call_args_list[0].args[0]
        self.assertEqual(event["type"], "execution_error")
        self.assertEqual(event["data"]["exception_message"], "worker stopped")
        self.assertEqual(event["data"]["node_id"], "")
        self.assertIn("create_time", job["history"]["prompt"][3])

    async def test_mode_switch_refuses_queued_jobs(self):
        self.control.journal.enqueue({"prompt": {"1": {}}})
        response = await self.client.post("/split/mode", json={"mode": "legacy"})
        self.assertEqual(response.status, 409)
        self.worker.spawn.aio.assert_not_awaited()

    async def test_legacy_uses_same_gpu_function_and_hides_token(self):
        response = await self.client.post("/split/mode", json={"mode": "legacy"})
        self.assertEqual(response.status, 202)
        session = self.control.journal.data["session"]
        self.assertEqual(session["status"], "queued")
        started = asyncio.Event()
        async def spawn(spec):
            started.set()
            return SimpleNamespace(object_id="fc-test")
        self.worker.spawn.aio.side_effect = spawn
        self.control.read_result = AsyncMock(return_value=None)
        self.control.cleanup_storage = AsyncMock()
        task = asyncio.create_task(self.control.dispatch())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.worker.spawn.aio.assert_awaited_once()
        spec = self.worker.spawn.aio.call_args.args[0]
        self.assertEqual(spec["operation"], "legacy")
        state = await (await self.client.get("/split/status")).json()
        self.assertNotIn(spec["token"], json.dumps(state))

    async def test_failed_environment_does_not_change_active_revision(self):
        self.control.journal.data["candidate"] = {"version": "env-broken", "status": "validating"}
        self.control.candidate = SimpleNamespace(url=self.control.cpu.url, stop=AsyncMock())
        # Missing verified Manager queue status must fail before GPU validation.
        with (patch("asyncio.create_subprocess_exec", side_effect=RuntimeError("bad dependency")),
              self.assertLogs("comfy_split.gateway", level="ERROR")):
            await self.control.apply_environment()
        self.assertEqual(self.control.journal.data["environment"], "base")
        self.assertEqual(self.control.journal.data["candidate"]["status"], "failed")
        self.worker.spawn.aio.assert_not_awaited()

    async def test_mode_is_not_ready_until_cpu_has_restarted(self):
        self.control.journal.data.update(mode="legacy", session={"stopping": True})
        ready = asyncio.Event()
        self.control.cpu.start.side_effect = lambda *_args, **_kwargs: None
        async def delayed_start(*_args, **_kwargs):
            await ready.wait()
        self.control.cpu.start.side_effect = delayed_start
        task = asyncio.create_task(self.control.end_legacy({"status": "completed"}))
        await asyncio.sleep(0)
        self.assertEqual(self.control.journal.data["mode"], "legacy")
        ready.set()
        await task
        self.assertEqual(self.control.journal.data["mode"], "split")

    async def test_environment_recovery_reuses_existing_gpu_result(self):
        self.control.journal.data["candidate"] = {"version": "env-test", "status": "validating"}
        self.control.journal.data["session"] = {"id": "validation", "operation": "validate",
            "call_id": "fc-existing", "result": {"status": "completed", "catalog": {"nodes": [], "objects": {}}}}
        self.control.candidate = SimpleNamespace(stop=AsyncMock(), start=AsyncMock(),
                                                catalog=AsyncMock(return_value={"nodes": []}))
        checked = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", None)))
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=checked)), \
             patch("comfy_split.gateway.environment_path", return_value=Path(self.temp.name) / "candidate"):
            await self.control.apply_environment(resume=True)
        self.assertEqual(self.control.journal.data["environment"], "env-test")
        self.assertIsNone(self.control.journal.data["session"])
        self.worker.spawn.aio.assert_not_awaited()

    async def test_plain_prompt_is_idempotent_and_optional_endpoints_are_absent(self):
        self.assertEqual(self.control.plugins, [])
        body = {"prompt": {"1": {"class_type": "SaveImage", "inputs": {}}}}
        responses = [await self.client.post("/prompt", json=body, headers={"Idempotency-Key": "plain"}) for _ in range(2)]
        self.assertEqual([r.status for r in responses], [200, 200])
        self.assertEqual((await responses[0].json())["prompt_id"], (await responses[1].json())["prompt_id"])
        for path in [route for source in EXTENSIONS.values() for route in source["guarded_routes"]] + [source["prefix"] + "catalog" for source in INTEGRATIONS.values()]:
            self.assertEqual((await self.client.get(path)).status, 404)
        self.worker.spawn.aio.assert_not_awaited()

    async def test_disabled_adapter_rejects_before_acceptance(self):
        response = await self.client.post("/prompt", json={"prompt": {
            "1": {"class_type": ADAPTER["node_prefix"] + "Text", "inputs": {}}}})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.control.journal.data["jobs"], {})
        self.worker.spawn.aio.assert_not_awaited()

    async def test_optional_observer_failure_does_not_block_native_events(self):
        def broken(_event):
            raise RuntimeError("Extension failure")
        self.control.plugins = [SimpleNamespace(event=broken)]
        socket = SimpleNamespace(send_json=AsyncMock())
        self.control.sockets = {"native": [socket]}
        event = {"type": "progress", "data": {"value": 1, "max": 2}}
        with self.assertLogs("comfy_split.gateway", level="ERROR"):
            await self.control.broadcast(event)
        socket.send_json.assert_awaited_once_with(event)

    async def test_extension_events_preserve_targeted_and_global_recipients(self):
        events = [{"type": name} for name in ("source", "derived", "nested")]
        def additional(event):
            return {"source": [events[1]], "derived": [events[2]]}.get(event["type"], [])
        self.control.plugins = [SimpleNamespace(event=additional)]
        first = SimpleNamespace(send_json=AsyncMock())
        second = SimpleNamespace(send_json=AsyncMock())
        self.control.sockets = {"first": [first], "second": [second]}
        for recipient in ("first", None):
            with self.subTest(recipient=recipient):
                first.send_json.reset_mock()
                second.send_json.reset_mock()
                await self.control.broadcast(events[0], recipient)
                delivered = [call.args[0] for call in first.send_json.call_args_list]
                self.assertEqual(delivered, list(reversed(events)))
                other = [call.args[0] for call in second.send_json.call_args_list]
                self.assertEqual(other, delivered if recipient is None else [])


class RoutingTests(unittest.TestCase):
    def test_api_prefix_normalization(self):
        self.assertEqual(api_path("/api/prompt"), "/prompt")
        self.assertEqual(api_path("/apiculture"), "/apiculture")
