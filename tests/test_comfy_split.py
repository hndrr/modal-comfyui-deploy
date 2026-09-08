import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from comfy_split.gateway import Controller, api_path
from comfy_split.state import Journal
from comfy_split import worker as worker_module


def remote_mock(result=None):
    return SimpleNamespace(aio=AsyncMock(return_value=result))


class JournalTests(unittest.TestCase):
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
            return web.json_response({"cpu": True})

        cpu_app = web.Application()
        cpu_app.router.add_route("*", "/{path:.*}", cpu_handler)
        self.cpu_server = TestServer(cpu_app)
        await self.cpu_server.start_server()
        self.control.cpu = SimpleNamespace(url=str(self.cpu_server.make_url("/"))[:-1],
                                           stop=AsyncMock(), start=AsyncMock())
        self.control.client = ClientSession(auto_decompress=False)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.control.handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.control.client.close()
        await self.cpu_server.close()

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

    async def test_cancel_pending_batch_does_not_wake_gpu(self):
        job = self.control.journal.enqueue({"prompt": {"1": {}}})
        response = await self.client.post("/api/jobs/cancel", json={"job_ids": [job["id"]]})
        self.assertEqual(response.status, 200)
        self.assertEqual(job["status"], "cancelled")
        self.worker.spawn.aio.assert_not_awaited()
        self.commands.put.aio.assert_not_awaited()

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
            await self.control.apply_environment()
        self.assertEqual(self.control.journal.data["environment"], "base")
        self.assertEqual(self.control.journal.data["candidate"]["status"], "failed")
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
            fake_process = SimpleNamespace(url=str(server.make_url("/"))[:-1],
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
            process = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), version=None)
            with patch.object(worker_module, "process", process), \
                 patch.object(worker_module, "Path", lambda _: Path(root)), \
                 patch.object(worker_module, "generate", AsyncMock(return_value={"status": "completed"})):
                result = await worker_module.run_worker({"id": "job", "environment": "base", "operation": "generate"},
                    SimpleNamespace(put=remote_mock()), SimpleNamespace(get_many=remote_mock([])), volumes)
            self.assertEqual(sequence, ["output", "result"])
            self.assertEqual(json.loads((Path(root) / "job.json").read_text())["status"], "completed")
            self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
