"""Exercise Ambient against the real split gateway; only Modal and ComfyUI are doubles."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import modal
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from ambient.comfy import generate
from ambient.readiness import check_comfyui
from ambient.split import SPLIT_HEADERS
from ambient_fixtures import object_info
from comfy_split.gateway import Controller
from test_ambient import request


def remote(result=None):
    return SimpleNamespace(aio=AsyncMock(return_value=result))


class SplitIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.destination = self.root / "video.mp4"
        self.worker = SimpleNamespace(
            spawn=remote(SimpleNamespace(object_id="fc-test")),
            get_current_stats=remote(SimpleNamespace(num_total_runners=0)),
        )
        self.commands = SimpleNamespace(put=remote())
        self.volumes = {
            name: SimpleNamespace(commit=remote(), reload=remote())
            for name in ("input", "output", "user", "models", "state", "results", "environment")
        }
        self.ui = SimpleNamespace(update_autoscaler=remote())
        self.control = Controller(
            self.worker,
            SimpleNamespace(get_many=remote([])),
            self.commands,
            self.volumes,
            self.root,
            ui_function=self.ui,
        )
        self.cancelled = False
        self.cancel_on_upload = False
        self.switch_on_catalog = False
        self.cpu_requests = []
        self.upload = None
        self.tasks = []
        self.queued = asyncio.Event()
        self.req = request()

        async def cpu_handler(req):
            self.cpu_requests.append(req.path)
            if req.path == "/ws":
                socket = web.WebSocketResponse()
                await socket.prepare(req)
                async for message in socket:
                    await socket.send_str(message.data)
                return socket
            if req.path == "/object_info":
                if self.switch_on_catalog:
                    self.control.journal.data["mode"] = "legacy"
                return web.json_response(object_info())
            if req.path == "/system_stats":
                return web.json_response({"system": {"comfyui_version": "test"}})
            if req.path == "/upload/image":
                form = await req.post()
                self.upload = form["image"].file.read()
                form["image"].file.close()
                self.cancelled = self.cancel_on_upload
                return web.json_response(
                    {"name": form["image"].filename, "subfolder": form["subfolder"]}
                )
            if req.path == "/view":
                # The real gateway must reload committed worker outputs before publishing history.
                self.volumes["output"].reload.aio.assert_awaited()
                return web.Response(body=b"generated video", content_type="video/mp4")
            raise web.HTTPNotFound()

        cpu_app = web.Application()
        cpu_app.router.add_route("*", "/{path:.*}", cpu_handler)
        self.cpu_server = TestServer(cpu_app)
        await self.cpu_server.start_server()
        self.control.cpu = SimpleNamespace(
            url=str(self.cpu_server.make_url("/")).rstrip("/"),
            stop=AsyncMock(),
            start=AsyncMock(),
            archive_temp=Mock(),
            dependencies={"comfy-kitchen": {
                "version": "0.2.33", "expected": "0.2.33", "missingApis": [],
            }},
        )
        self.control.client = ClientSession(auto_decompress=False)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.control.handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.base = str(self.client.make_url("/")).rstrip("/")

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.close()
        await self.control.client.close()
        await self.cpu_server.close()

    def progress(self, stage):
        if stage == "ComfyUI queued":
            self.queued.set()

    async def generate(self, image=None):
        await generate(
            self.base,
            {},
            self.req,
            image,
            self.destination,
            lambda: self.cancelled,
            self.progress,
            timeout=5,
        )

    async def start_generation(self, image=None):
        task = asyncio.create_task(self.generate(image))
        self.tasks.append(task)
        await asyncio.wait_for(self.queued.wait(), 2)
        job = next(
            j for j in self.control.journal.data["jobs"].values() if "15" in j["body"]["prompt"]
        )
        return task, job

    async def test_open_ui_and_inventory_do_not_spawn_gpu(self):
        async with self.client.ws_connect("/ws?clientId=browser") as socket:
            await socket.receive_json()
            record = await check_comfyui(self.base, {})
            self.assertEqual(record["backend"], "split")
            self.assertEqual(record["environment"], "base")
            self.assertFalse(record["gpuValidated"])
            self.worker.spawn.aio.assert_not_awaited()
            self.assertFalse(socket.closed)

    async def test_fast_comfy_inventory_and_generation_use_the_existing_gateway(self):
        self.req = request(mode="fasth3", backend="comfyui")
        record = await check_comfyui(self.base, {}, "fasth3")
        self.assertEqual(record["dependencies"]["comfy-kitchen"]["version"], "0.2.33")
        self.assertFalse(record["gpuValidated"])
        self.worker.spawn.aio.assert_not_awaited()
        task, job = await self.start_generation()
        graph = job["body"]["prompt"]
        self.assertEqual(graph["2"]["class_type"], "BlockSparseAttention")
        self.assertEqual(graph["2"]["inputs"]["selection.keep_percent"], 10)
        self.assertIsNone(self.upload)
        call = SimpleNamespace(get=remote({"status": "completed", "history": {
            "status": {"completed": True},
            "outputs": {"15": {"images": [{"filename": "video.mp4", "type": "output"}]}},
        }}))
        with patch("comfy_split.gateway.modal.FunctionCall.from_id", return_value=call):
            dispatcher = asyncio.create_task(self.control.dispatch())
            self.tasks.append(dispatcher)
            await asyncio.wait_for(task, 3)
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        self.assertEqual(self.destination.read_bytes(), b"generated video")
        self.worker.spawn.aio.assert_awaited_once()

    async def test_fast_comfy_dependency_failure_never_submits_a_gpu_job(self):
        self.req = request(mode="fasth3", backend="comfyui")
        self.control.cpu.dependencies["comfy-kitchen"]["version"] = "0.2.1"
        with self.assertRaisesRegex(ValueError, "comfy-kitchen"):
            await check_comfyui(self.base, {}, "fasth3")
        with self.assertRaisesRegex(ValueError, "comfy-kitchen"):
            await self.generate()
        self.worker.spawn.aio.assert_not_awaited()
        self.assertEqual(self.cpu_requests, [])

    async def test_fast_comfy_queued_cancellation_is_scoped(self):
        self.req = request(mode="fasth3", backend="comfyui")
        await self.test_cancel_queued_job_preserves_other_browser_job()

    async def test_fast_comfy_running_cancellation_is_scoped(self):
        self.req = request(mode="fasth3", backend="comfyui")
        await self.test_cancel_running_job_sends_only_its_scoped_interrupt()

    async def test_anchor_generation_dispatches_once_and_downloads_while_ui_stays_open(self):
        anchor = self.root / "anchor.png"
        anchor.write_bytes(b"anchor image")
        result = {
            "status": "completed",
            "history": {
                "status": {"completed": True},
                "outputs": {"15": {"images": [{"filename": "video.mp4", "type": "output"}]}},
            },
        }
        call = SimpleNamespace(get=remote(result))
        async with self.client.ws_connect("/ws?clientId=browser") as socket:
            await socket.receive_json()
            task, job = await self.start_generation(anchor)
            self.worker.spawn.aio.assert_not_awaited()
            with patch("comfy_split.gateway.modal.FunctionCall.from_id", return_value=call):
                dispatcher = asyncio.create_task(self.control.dispatch())
                self.tasks.append(dispatcher)
                await asyncio.wait_for(task, 3)
                dispatcher.cancel()
                await asyncio.gather(dispatcher, return_exceptions=True)
            self.assertEqual(self.destination.read_bytes(), b"generated video")
            self.assertEqual(self.upload, b"anchor image")
            self.assertEqual(
                job["body"]["prompt"]["16"]["inputs"]["image"],
                f"ambient/uploads/ambient-{self.req['requestId']}.png",
            )
            self.worker.spawn.aio.assert_awaited_once()
            spec = self.worker.spawn.aio.call_args.args[0]
            self.assertEqual(spec["id"], job["id"])
            self.assertEqual(spec["operation"], "generate")
            self.assertEqual(job["status"], "completed")
            self.assertFalse(self.control.background_work())
            await self.control.reconcile_cpu_scaling()
            self.ui.update_autoscaler.aio.assert_awaited_with(min_containers=0)
            self.assertFalse(socket.closed)
            self.assertIn("browser", self.control.sockets)

    async def test_cancel_queued_job_preserves_other_browser_job(self):
        other = self.control.journal.enqueue({"prompt": {"1": {}}})
        task, own = await self.start_generation()
        self.cancelled = True
        await asyncio.wait_for(task, 3)
        self.assertEqual(own["status"], "cancelled")
        self.assertEqual(other["status"], "queued")
        self.commands.put.aio.assert_not_awaited()
        self.worker.spawn.aio.assert_not_awaited()

    async def test_gpu_execution_timeout_is_published_as_failure(self):
        task, own = await self.start_generation()
        call = SimpleNamespace(get=remote())
        call.get.aio.side_effect = modal.exception.FunctionTimeoutError("GPU execution timed out")
        with patch("comfy_split.gateway.modal.FunctionCall.from_id", return_value=call):
            dispatcher = asyncio.create_task(self.control.dispatch())
            self.tasks.append(dispatcher)
            with self.assertRaisesRegex(RuntimeError, "GPU execution timed out"):
                await asyncio.wait_for(task, 3)
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        self.assertEqual(own["status"], "failed")
        self.assertFalse(self.control.background_work())
        self.worker.spawn.aio.assert_awaited_once()

    async def test_cancel_running_job_sends_only_its_scoped_interrupt(self):
        task, own = await self.start_generation()
        async with self.control.lock:
            await self.control.spawn(own, "generate")
            other = self.control.journal.enqueue({"prompt": {"1": {}}})
        self.cancelled = True
        await asyncio.wait_for(task, 3)
        self.commands.put.aio.assert_awaited_once_with({"type": "interrupt"}, partition=own["id"])
        self.assertEqual(other["status"], "queued")
        self.assertNotIn("/interrupt", self.cpu_requests)
        self.assertFalse(self.destination.exists())

    async def test_browser_cancellation_does_not_leave_ambient_polling_until_timeout(self):
        task, own = await self.start_generation()
        async with self.control.lock:
            await self.control.cancel([own["id"]])
        with self.assertRaisesRegex(RuntimeError, "left the queue without a result"):
            await asyncio.wait_for(task, 3)
        self.worker.spawn.aio.assert_not_awaited()

    async def test_cancellation_during_upload_does_not_enqueue(self):
        self.cancel_on_upload = True
        anchor = self.root / "anchor.png"
        anchor.write_bytes(b"anchor image")
        await self.generate(anchor)
        self.assertEqual(self.control.journal.data["jobs"], {})
        self.worker.spawn.aio.assert_not_awaited()

    async def test_legacy_mode_is_rejected_before_comfyui_requests(self):
        self.control.journal.data["mode"] = "legacy"
        for operation in (lambda: check_comfyui(self.base, {}), self.generate):
            with self.assertRaisesRegex(RuntimeError, "requires splitapp in split mode"):
                await operation()
        self.assertEqual(self.cpu_requests, [])
        self.worker.spawn.aio.assert_not_awaited()

    async def test_mode_change_after_preflight_cannot_route_prompt_to_legacy_gpu(self):
        self.switch_on_catalog = True
        with self.assertRaisesRegex(RuntimeError, "HTTP 409"):
            await self.generate()
        self.assertEqual(self.control.journal.data["jobs"], {})
        self.assertNotIn("/prompt", self.cpu_requests)
        self.worker.spawn.aio.assert_not_awaited()

    async def test_guarded_legacy_requests_are_rejected_without_changing_normal_ui(self):
        self.control.journal.data.update(
            mode="legacy", session={"url": self.control.cpu.url, "token": "test"}
        )
        for path in ("/api/object_info", "/ws", "/prompt", "/jobs/job/cancel"):
            response = await self.client.post(path, json={}, headers=SPLIT_HEADERS)
            self.assertEqual(response.status, 409)
        self.assertEqual(self.cpu_requests, [])
        response = await self.client.get("/object_info")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.cpu_requests, ["/object_info"])

    async def test_standard_comfyui_endpoint_is_rejected(self):
        self.base = self.control.cpu.url
        with self.assertRaisesRegex(RuntimeError, "splitapp CPU UI endpoint"):
            await self.generate()
        self.assertEqual(self.cpu_requests, ["/modal-control/v1/status"])


if __name__ == "__main__":
    unittest.main()
