import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from comfy_split.gateway import Controller, api_path
from comfy_split.state import Journal
from comfy_split import worker as worker_module
from comfy_split.check_environment import check_pins


def remote_mock(result=None):
    return SimpleNamespace(aio=AsyncMock(return_value=result))


class JournalTests(unittest.TestCase):
    def test_candidate_rejects_install_scripts_that_override_protected_packages(self):
        check_pins("torch==2.10.0+cu130\n", lambda _: "2.10.0+cu130")
        with self.assertRaisesRegex(RuntimeError, "固定依存の競合"):
            check_pins("torch==2.10.0+cu130\n", lambda _: "2.11.0")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name))

    def test_idempotency_survives_restart_and_rejects_different_body(self):
        body = {"prompt": {"1": {"class_type": "Test"}}}
        first = self.journal.enqueue(body, "retry")
        self.journal.save()
        journal = Journal(Path(self.temp.name))
        self.assertEqual(journal.enqueue(body, "retry")["id"], first["id"])
        with self.assertRaises(ValueError):
            journal.enqueue({"prompt": {"different": {}}}, "retry")

    def test_unknown_dispatch_never_returns_to_queue(self):
        job = self.journal.enqueue({"prompt": {"1": {}}})
        job["status"] = "dispatching"
        self.journal.save()
        restarted = Journal(Path(self.temp.name))
        restarted.recover()
        self.assertEqual(restarted.data["jobs"][job["id"]]["status"], "unknown")
        self.assertIsNone(restarted.next_job())
        with self.assertRaises(ValueError):
            restarted.assert_idle()

    def test_running_call_is_recoverable_without_resubmission(self):
        job = self.journal.enqueue({"prompt": {"1": {}}})
        job.update(status="running", call_id="fc-existing")
        self.journal.save()
        restarted = Journal(Path(self.temp.name))
        restarted.recover()
        self.assertIsNone(restarted.next_job())
        self.assertEqual(restarted.data["jobs"][job["id"]]["call_id"], "fc-existing")

    def test_environment_and_mode_changes_reject_pending_work(self):
        self.journal.enqueue({"prompt": {"1": {}}})
        with self.assertRaises(ValueError):
            self.journal.assert_idle()
        self.journal.data["candidate"] = {"version": "env-new"}
        with self.assertRaises(ValueError):
            self.journal.enqueue({"prompt": {"2": {}}})


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

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.worker = SimpleNamespace(spawn=remote_mock(SimpleNamespace(object_id="fc-test")))
        self.events = SimpleNamespace(get_many=remote_mock([]))
        self.commands = SimpleNamespace(put=remote_mock())
        self.volumes = {key: SimpleNamespace(commit=remote_mock(), reload=remote_mock())
                        for key in ("input", "output", "user", "models", "state", "results", "environment")}
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
            if request.path == "/object_info":
                return web.json_response({"Test": {"input": {}}})
            if request.path == "/extensions":
                return web.json_response([])
            if request.path == "/_split/jobs":
                self.job_snapshot = await request.json()
                return web.json_response({"jobs": [], "pagination": {"total": 0}})
            return web.json_response({"cpu": True})

        cpu_app = web.Application()
        cpu_app.router.add_route("*", "/{path:.*}", cpu_handler)
        self.cpu_server = TestServer(cpu_app)
        await self.cpu_server.start_server()
        self.control.cpu = SimpleNamespace(url=str(self.cpu_server.make_url("/"))[:-1],
                                           stop=AsyncMock(), start=AsyncMock(), archive_temp=Mock())
        self.control.client = ClientSession(auto_decompress=False)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.control.handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.control.client.close()
        await self.cpu_server.close()

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

    async def test_image_browsing_repair_rejects_queued_generation(self):
        self.control.journal.enqueue({"prompt": {"1": {}}})
        response = await self.client.post("/api/split/environment/repair-image-browsing")
        self.assertEqual(response.status, 409)
        self.assertIsNone(self.control.journal.data["candidate"])

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
        self.assertEqual(self.volumes["state"].commit.aio.await_count, 3)
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
        exec(compile("def fail():\n raise RuntimeError('remote failure')", "<ta-test>:/root/worker.py", "exec"), namespace)
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

    async def test_spawn_failure_keeps_dispatch_intent_and_prevents_second_job(self):
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        self.worker.spawn.aio.side_effect = RuntimeError("connection lost after send")
        with self.assertRaises(RuntimeError):
            await self.control.spawn(job, "generate")
        disk = Journal(Path(self.temp.name))
        disk.recover()
        self.assertEqual(disk.data["jobs"][job["id"]]["status"], "unknown")
        self.assertIsNone(disk.next_job())

    async def test_completion_waits_for_output_visibility(self):
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        job["status"] = "running"
        history = {"outputs": {}, "status": {"status_str": "success"}}
        ready = asyncio.Event()
        self.volumes["output"].reload.aio.side_effect = ready.wait
        finishing = asyncio.create_task(self.control.finish(job, {"status": "completed", "history": history}))
        await asyncio.sleep(0)
        self.assertEqual(job["status"], "running")
        ready.set()
        await finishing
        self.assertEqual(job["status"], "completed")
        self.assertEqual(Journal(Path(self.temp.name)).history()[job["id"]], history)

    async def test_mode_switch_refuses_queued_jobs(self):
        self.control.journal.enqueue({"prompt": {"1": {}}})
        response = await self.client.post("/split/mode", json={"mode": "legacy"})
        self.assertEqual(response.status, 409)
        self.worker.spawn.aio.assert_not_awaited()

    async def test_legacy_uses_same_gpu_function_and_hides_token(self):
        response = await self.client.post("/split/mode", json={"mode": "legacy"})
        self.assertEqual(response.status, 202)
        self.worker.spawn.aio.assert_awaited_once()
        spec = self.worker.spawn.aio.call_args.args[0]
        self.assertEqual(spec["operation"], "legacy")
        state = await (await self.client.get("/split/status")).json()
        self.assertNotIn(spec["token"], json.dumps(state))

    async def test_failed_environment_does_not_change_active_revision(self):
        self.control.journal.data["candidate"] = {"version": "env-broken", "status": "validating"}
        self.control.candidate = SimpleNamespace(url=self.control.cpu.url, stop=AsyncMock())
        # Missing verified Manager queue status must fail before GPU validation.
        with patch("asyncio.create_subprocess_exec", side_effect=RuntimeError("bad dependency")):
            with self.assertLogs("comfy_split.gateway", level="ERROR"):
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


class RoutingTests(unittest.TestCase):
    def test_api_prefix_normalization(self):
        self.assertEqual(api_path("/api/prompt"), "/prompt")
        self.assertEqual(api_path("/apiculture"), "/apiculture")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_comfy_protocol_submits_once_relays_and_interrupts(self):
        state = {"submitted": 0, "interrupted": False, "history_reads": 0}
        sockets = []

        async def ws(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            sockets.append(socket)
            async for _ in socket:
                pass
            return socket

        async def prompt(request):
            body = await request.json()
            state["submitted"] += 1
            self.assertEqual(body["client_id"], "job-1")
            for socket in sockets:
                await socket.send_json({"type": "progress", "data": {"value": 1, "max": 2}})
                await socket.send_bytes(b"preview")
            return web.json_response({"prompt_id": body["prompt_id"]})

        async def interrupt(request):
            state["interrupted"] = True
            return web.json_response({})

        async def history(request):
            state["history_reads"] += 1
            if state["history_reads"] < 2:
                return web.json_response({})
            return web.json_response({"job-1": {"outputs": {}, "status": {
                "status_str": "error", "messages": [["execution_interrupted", {}]]}}})

        app = web.Application()
        app.router.add_get("/ws", ws)
        app.router.add_post("/prompt", prompt)
        app.router.add_post("/interrupt", interrupt)
        app.router.add_get("/history/{id}", history)
        server = TestServer(app)
        await server.start_server()
        try:
            fake_process = SimpleNamespace(archive_temp=Mock(), durable_outputs=lambda x: x, url=str(server.make_url("/"))[:-1],
                                           process=SimpleNamespace(returncode=None))
            emit = AsyncMock()
            with patch.object(worker_module, "process", fake_process):
                async with ClientSession() as client:
                    result = await worker_module.generate(
                        {"id": "job-1", "body": {"prompt": {"1": {}}}}, client,
                        AsyncMock(return_value=[{"type": "interrupt"}]), emit)
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(state["submitted"], 1)
            self.assertTrue(state["interrupted"])
            types = [call.args[0]["type"] for call in emit.call_args_list]
            self.assertIn("event", types)
            self.assertIn("preview", types)
        finally:
            await server.close()

    async def test_result_receipt_is_written_after_outputs_committed(self):
        sequence = []
        with tempfile.TemporaryDirectory() as root:
            volumes = {key: SimpleNamespace(commit=remote_mock(), reload=remote_mock())
                       for key in ("environment", "input", "models", "user", "results", "output")}
            async def output_commit():
                sequence.append("output")
            async def result_commit():
                sequence.append("result")
            volumes["output"].commit.aio.side_effect = output_commit
            volumes["results"].commit.aio.side_effect = result_commit
            process = SimpleNamespace(archive_temp=Mock(), durable_outputs=lambda x: x, start=AsyncMock(), stop=AsyncMock(), version=None)
            with patch.object(worker_module, "process", process), \
                 patch.object(worker_module, "Path", lambda _: Path(root)), \
                 patch.object(worker_module, "generate", AsyncMock(return_value={"status": "completed"})):
                result = await worker_module.run_worker({"id": "job", "environment": "base", "operation": "generate"},
                    SimpleNamespace(put=remote_mock()), SimpleNamespace(get_many=remote_mock([])), volumes)
            self.assertEqual(sequence, ["result", "output", "result"])
            self.assertEqual(json.loads((Path(root) / "job.json").read_text())["status"], "completed")
            self.assertEqual(result["status"], "completed")

    async def test_preempted_input_with_start_receipt_is_not_reexecuted(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "job.started.json").write_text("{}")
            volumes = {key: SimpleNamespace(commit=remote_mock(), reload=remote_mock())
                       for key in ("environment", "input", "models", "user", "results", "output")}
            process = SimpleNamespace(archive_temp=Mock(), durable_outputs=lambda x: x, start=AsyncMock(), stop=AsyncMock(), version=None)
            generate = AsyncMock()
            with patch.object(worker_module, "process", process), \
                 patch.object(worker_module, "Path", lambda _: Path(root)), \
                 patch.object(worker_module, "generate", generate):
                result = await worker_module.run_worker({"id": "job", "environment": "base", "operation": "generate"},
                    SimpleNamespace(put=remote_mock()), SimpleNamespace(get_many=remote_mock([])), volumes)
            self.assertEqual(result["status"], "unknown")
            generate.assert_not_awaited()
            process.start.assert_not_awaited()

    async def test_warm_worker_skips_immutable_environment_and_closes_mapped_files(self):
        with tempfile.TemporaryDirectory() as root:
            volumes = {key: SimpleNamespace(commit=remote_mock(), reload=remote_mock())
                       for key in ("environment", "input", "models", "user", "results", "output")}
            volumes["models"].reload.aio.side_effect = [RuntimeError("there are open files preventing the operation"), None]
            process = SimpleNamespace(archive_temp=Mock(), durable_outputs=lambda x: x, start=AsyncMock(), stop=AsyncMock(), version="base")
            with patch.object(worker_module, "process", process), \
                 patch.object(worker_module, "Path", lambda _: Path(root)), \
                 patch.object(worker_module, "generate", AsyncMock(return_value={"status": "completed"})):
                result = await worker_module.run_worker({"id": "warm", "environment": "base", "operation": "generate"},
                    SimpleNamespace(put=remote_mock()), SimpleNamespace(get_many=remote_mock([])), volumes)
            self.assertEqual(result["status"], "completed")
            volumes["environment"].reload.aio.assert_not_awaited()
            self.assertEqual(volumes["models"].reload.aio.await_count, 2)
            process.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
