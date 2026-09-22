import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from comfy_split.gateway import Controller
from comfy_split.startup import StartupGate


class StartupGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

        async def initialize(app):
            self.entered.set()
            await self.release.wait()

        self.controller = SimpleNamespace(start=AsyncMock(side_effect=initialize),
            pin_cpu=AsyncMock(), reconcile_cpu_scaling=AsyncMock(), close=AsyncMock(),
            startup_phase="updating_nodes", starting=False,
            handle=AsyncMock(side_effect=lambda request: web.json_response({"comfy": True})))
        self.gate = StartupGate(self.controller, request_wait=0.02)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.gate.handle)
        app.on_startup.append(self.gate.start)
        app.on_cleanup.append(self.gate.close)
        self.client = TestClient(TestServer(app))
        await asyncio.wait_for(self.client.start_server(), 1)
        await self.entered.wait()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_slow_start_serves_progress_and_rejects_unaccepted_jobs_until_ready(self):
        self.assertTrue(self.controller.starting)
        response = await asyncio.wait_for(self.client.get("/"), 1)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("ComfyUIを準備しています", await response.text())
        status = await (await self.client.get("/split/startup")).json()
        self.assertFalse(status["ready"])
        self.assertEqual(status["stage"], "updating_nodes")
        response = await self.client.post("/prompt", json={"prompt": {}})
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["Retry-After"], "2")
        self.controller.handle.assert_not_awaited()
        self.assertFalse(self.gate.task.done())
        self.release.set()
        await self.gate.finished.wait()
        self.assertFalse(self.controller.starting)
        self.assertTrue((await (await self.client.get("/api/split/startup")).json())["ready"])
        self.assertEqual(await (await self.client.get("/")).json(), {"comfy": True})
        self.controller.start.assert_awaited_once()
        self.controller.reconcile_cpu_scaling.assert_awaited_once()

    async def test_api_waits_for_short_start_and_disconnect_does_not_cancel_startup(self):
        self.gate.request_wait = 10
        request = asyncio.create_task(self.client.get("/object_info"))
        await asyncio.sleep(0.02)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertFalse(self.gate.task.done())
        pending = asyncio.create_task(self.client.get("/object_info"))
        await asyncio.sleep(0.02)
        self.release.set()
        response = await asyncio.wait_for(pending, 1)
        self.assertEqual(response.status, 200)
        self.controller.start.assert_awaited_once()

    async def test_failure_is_visible_without_leaking_exception_or_looping_startup(self):
        async def fail(app):
            await self.release.wait()
            raise RuntimeError("private-path credential-detail")

        # Replace the already-running initialization's release with an error.
        self.gate.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.gate.task
        self.gate.finished.clear()
        self.controller.start.side_effect = fail
        with self.assertLogs("comfy_split.startup", level="ERROR"):
            await self.gate.start(None)
            self.release.set()
            await self.gate.finished.wait()
        response = await self.client.get("/object_info")
        self.assertEqual(response.status, 503)
        self.assertNotIn("credential-detail", await response.text())
        self.assertTrue((await (await self.client.get("/split/startup")).json())["failed"])
        self.assertFalse(self.controller.starting)
        self.controller.handle.assert_not_awaited()


class StartupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ui = SimpleNamespace(update_autoscaler=SimpleNamespace(aio=AsyncMock()))
        volumes = {key: SimpleNamespace(commit=SimpleNamespace(aio=AsyncMock()))
                   for key in ("data", "environment")}
        self.controller = Controller(Mock(), None, None, volumes, Path(self.directory.name),
                                     ui_function=self.ui)

    async def test_node_refresh_cannot_release_cpu_during_remaining_initialization(self):
        self.controller.starting = True
        await self.controller.pin_cpu(True)
        await self.controller.pin_cpu(False)
        self.ui.update_autoscaler.aio.assert_awaited_once_with(min_containers=1)
        self.controller.starting = False
        await self.controller.reconcile_cpu_scaling()
        self.assertEqual(self.ui.update_autoscaler.aio.call_args.kwargs, {"min_containers": 0})

    async def test_startup_does_not_wait_for_retention_cleanup(self):
        self.controller.refresh_ambient_nodes = AsyncMock()
        self.controller.cleanup_storage = AsyncMock()
        self.controller.cpu.start = AsyncMock()
        self.controller.dispatch = AsyncMock()
        with patch("comfy_split.gateway.initialize_environment"):
            await self.controller.start(None)
        self.controller.cpu.start.assert_awaited_once_with("base", cpu=True)
        self.controller.cleanup_storage.assert_not_awaited()
        self.assertGreater(self.controller.cleanup_due, 0)
        await self.controller.close(None)
