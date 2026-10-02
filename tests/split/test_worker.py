import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from comfy_split import worker as worker_module
from comfy_split.config import Settings
from tests.split.support import remote_mock, volume_mocks


class WorkerProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings = patch.dict(os.environ, Settings().environment())
        settings.start()
        self.addCleanup(settings.stop)

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
            async def tail():
                # Native history is committed just before the final WS frames.
                await asyncio.sleep(0.02)
                for socket in sockets:
                    await socket.send_json({"type": "progress", "data": {"value": 2, "max": 2}})
                    await socket.send_json({"type": "executing", "data": {"node": None, "prompt_id": "job-1"}})
            asyncio.create_task(tail())
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
            progress = [call.args[0]["event"]["data"]["value"] for call in emit.call_args_list
                        if call.args[0].get("event", {}).get("type") == "progress"]
            self.assertEqual(progress, [1, 2])
        finally:
            await server.close()


class WorkerExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, Settings().environment()))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.volumes = volume_mocks()
        self.process = SimpleNamespace(archive_temp=Mock(), durable_outputs=lambda x: x,
            url="http://unused.invalid", start=AsyncMock(), stop=AsyncMock(), version=None)
        self.generate = AsyncMock(return_value={"status": "completed"})
        self.enterContext(patch.object(worker_module, "process", self.process))
        self.enterContext(patch.object(worker_module, "JOBS", self.root))
        self.enterContext(patch.object(worker_module, "generate", self.generate))

    async def run_job(self):
        return await worker_module.run_worker(
            {"id": "job", "environment": "base", "operation": "generate"},
            SimpleNamespace(put=remote_mock()), SimpleNamespace(get_many=remote_mock([])), self.volumes)

    async def test_result_receipt_is_written_after_outputs_committed(self):
        sequence = []
        self.volumes["output"].commit.aio.side_effect = lambda: sequence.append("output")
        self.volumes["data"].commit.aio.side_effect = lambda: sequence.append("result")
        result = await self.run_job()
        self.assertEqual(sequence, ["result", "output", "result"])
        self.assertEqual(json.loads((self.root / "job.json").read_text())["status"], "completed", result)
        self.assertEqual(result["status"], "completed", result)

    async def test_preempted_input_with_start_receipt_is_not_reexecuted(self):
        (self.root / "job.started.json").write_text("{}")
        result = await self.run_job()
        self.assertEqual(result["status"], "unknown")
        self.generate.assert_not_awaited()
        self.process.start.assert_not_awaited()

    async def test_output_commit_failure_does_not_publish_completion(self):
        self.volumes["output"].commit.aio.side_effect = OSError("output commit failed")
        with self.assertRaisesRegex(OSError, "output commit failed"):
            await self.run_job()
        self.assertTrue((self.root / "job.started.json").exists())
        self.assertFalse((self.root / "job.json").exists())

    async def test_warm_worker_skips_immutable_environment_and_closes_mapped_files(self):
        self.process.version = "base"
        self.volumes["models"].reload.aio.side_effect = [RuntimeError("there are open files preventing the operation"), None]
        result = await self.run_job()
        self.assertEqual(result["status"], "completed", result)
        self.volumes["environment"].reload.aio.assert_not_awaited()
        self.assertEqual(self.volumes["models"].reload.aio.await_count, 2)
        self.process.stop.assert_awaited_once()
