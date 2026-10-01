import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

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
            if request.path == "/object_info":
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

    async def test_spawn_failure_keeps_dispatch_intent_and_prevents_second_job(self):
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        self.worker.spawn.aio.side_effect = RuntimeError("connection lost after send")
        with self.assertRaises(RuntimeError):
            await self.control.spawn(job, "generate")
        disk = Journal(Path(self.temp.name))
        disk.recover()
        self.assertEqual(disk.data["jobs"][job["id"]]["status"], "unknown")
        self.assertIsNone(disk.next_job())


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
