"""Real split gateway/Journal/worker hooks with the optional Bridge installed."""
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from comfyui_agent_bridge.split import PREFIX, TOKEN_ENV
from comfy_split.gateway import Controller
from comfy_split.worker import generate_with_extensions


def remote(value=None):
    return SimpleNamespace(aio=AsyncMock(return_value=value))


HELLO = {"type": "hello", "version": 1, "worker_id": "mac-test", "catalog": {
    "models": ["test-model"], "skills": [], "revision": "test", "auth": "authenticated"}}


class NativeBridge:
    """Native peer for split's catalog, connection and worker-hook integration."""
    def __init__(self):
        self.socket = None
        self.hello = None

    async def handler(self, request):
        if request.path == PREFIX + "/catalog":
            return web.json_response({"connected": self.socket is not None,
                                      **(self.hello or {}).get("catalog", {})})
        if request.headers.get("Authorization") != "Bearer shared-test-token":
            raise web.HTTPUnauthorized()
        if request.path == PREFIX + "/ws":
            socket = web.WebSocketResponse(max_msg_size=20 * 1024 * 1024)
            await socket.prepare(request)
            self.socket = socket
            try:
                self.hello = await socket.receive_json()
                if self.hello.get("type") != "hello" or self.hello.get("version") != 1:
                    await socket.close(code=1008)
                    return socket
                await socket.send_json({"type": "ready", "version": 1})
                async for message in socket:
                    data = json.loads(message.data)
                    if data["type"] == "catalog":
                        self.hello["catalog"] = data["catalog"]
            finally:
                self.socket = None
            return socket
        raise web.HTTPNotFound()


class GatewayBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {TOKEN_ENV: "shared-test-token", "SPLIT_AGENT_BRIDGE": "on", "SPLIT_EXTENSIONS": "", "SPLIT_NODE_PACKS": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.servers = []
        self.cpu, self.gpu = NativeBridge(), NativeBridge()
        self.cpu_url = await self.serve(self.cpu.handler)
        self.gpu_url = await self.serve(self.gpu.handler)
        self.controller = Controller(SimpleNamespace(spawn=remote()),
            SimpleNamespace(get_many=remote([])), SimpleNamespace(put=remote()),
            {key: SimpleNamespace(commit=remote(), reload=remote())
             for key in ("environment", "data", "input", "models", "output")}, Path(self.temp.name))
        self.controller.cpu = SimpleNamespace(url=self.cpu_url, stop=AsyncMock())
        self.bridge = next(p for p in self.controller.plugins if hasattr(p, "identity"))
        self.controller.client = ClientSession()
        app = web.Application(client_max_size=256 * 1024 * 1024)
        app.router.add_route("*", "/{path:.*}", self.controller.handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.mac = None

    async def serve(self, handler):
        app = web.Application(client_max_size=256 * 1024 * 1024)
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        self.servers.append(server)
        return str(server.make_url("/")).rstrip("/")

    async def asyncTearDown(self):
        if self.mac is not None:
            await self.mac.close()
        await self.bridge.close()
        await self.client.close()
        await self.controller.client.close()
        for server in self.servers:
            await server.close()

    async def connect_mac(self):
        self.mac = await self.client.ws_connect(PREFIX + "/ws", headers={
            "Authorization": "Bearer shared-test-token"}, max_msg_size=20 * 1024 * 1024)
        await self.mac.send_json(HELLO)
        self.assertEqual(await self.mac.receive_json(timeout=2), {"type": "ready", "version": 1})


    async def test_catalog_connection_and_auth_do_not_start_gpu(self):
        response = await self.client.get(PREFIX + "/ws")
        self.assertEqual(response.status, 401)
        await self.connect_mac()
        catalog = await (await self.client.get("/api" + PREFIX + "/catalog")).json()
        self.assertTrue(catalog["connected"])
        self.assertEqual(catalog["models"], ["test-model"])
        with self.assertRaises(WSServerHandshakeError) as error:
            await self.client.ws_connect(PREFIX + "/ws", headers={"Authorization": "Bearer shared-test-token"})
        self.assertEqual(error.exception.status, 409)
        response = await self.client.post("/split/mode", json={"mode": "legacy"})
        self.assertEqual(response.status, 409)
        self.controller.worker.spawn.aio.assert_not_awaited()

    async def test_prompt_binds_connection_and_rejects_disconnected_mac(self):
        body = {"prompt": {"1": {"class_type": "AgentRuntimeBridgeText"}}}
        self.assertEqual((await self.client.post("/prompt", json=body)).status, 409)
        self.assertFalse(self.controller.journal.data["jobs"])
        await self.connect_mac()
        headers = {"Idempotency-Key": "bridge-request"}
        accepted = await (await self.client.post("/prompt", json=body, headers=headers)).json()
        job = self.controller.journal.data["jobs"][accepted["prompt_id"]]
        self.assertEqual(job["agent_bridge"], self.bridge.identity())
        self.bridge.connection_id = "different-connection"
        await self.controller.spawn(job, "generate")
        self.assertEqual(job["status"], "failed")
        self.controller.worker.spawn.aio.assert_not_awaited()
        self.bridge.connection_id = None
        retry = await (await self.client.post("/prompt", json=body, headers=headers)).json()
        self.assertEqual(retry["prompt_id"], job["id"])
        self.assertEqual(len(self.controller.journal.data["jobs"]), 1)

    async def test_admission_is_locked_and_saved_connection_survives_recovery(self):
        from comfy_split.state import Journal
        await self.connect_mac()
        original = self.bridge.admit
        def admit(body, existing):
            self.assertTrue(self.controller.lock.locked())
            return original(body, existing)
        with patch.object(self.bridge, "admit", side_effect=admit):
            response = await self.client.post("/prompt", json={"prompt": {
                "1": {"class_type": "AgentRuntimeBridgeMedia", "inputs": {}}}})
        record = self.controller.journal.data["jobs"][(await response.json())["prompt_id"]]
        frozen = record["agent_bridge"]
        record["status"] = "dispatching"
        self.controller.journal.save()
        restored = Journal(Path(self.temp.name))
        restored.recover()
        recovered = restored.data["jobs"][record["id"]]
        self.assertEqual(recovered["agent_bridge"], frozen)
        self.assertEqual(recovered["status"], "unknown")
        self.assertEqual(restored.next_job(), None)
        self.controller.worker.spawn.aio.assert_not_awaited()

    async def test_worker_control_hook_attaches_before_ack_and_finish_cleans_up(self):
        await self.connect_mac()
        job = self.controller.journal.enqueue({"prompt": {"1": {"class_type": "AgentRuntimeBridgeText"}}})
        job["agent_bridge"] = self.bridge.identity()
        await self.controller.relay_event(job, {"type": "agent_bridge_ready", "url": self.gpu_url,
                                                "token": "shared-test-token"})
        self.assertEqual(self.gpu.hello["worker_id"], "mac-test")
        self.controller.commands.put.aio.assert_awaited_once_with(
            {"type": "agent_bridge_connected"}, partition=job["id"])
        await self.controller.finish(job, {"status": "cancelled"})
        self.assertIsNone(self.bridge.url)
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(self.mac.closed)

    async def test_idle_disconnect_requires_auth_and_preserves_accepted_bridge_work(self):
        path = PREFIX + "/idle"
        headers = {"Authorization": "Bearer shared-test-token"}
        self.assertEqual((await self.client.post(path)).status, 401)
        await self.connect_mac()
        identity = self.bridge.identity()
        body = {"prompt": {"1": {"class_type": "AgentRuntimeBridgeText"}}}
        accepted = await (await self.client.post("/prompt", json=body)).json()
        record = self.controller.journal.data["jobs"][accepted["prompt_id"]]
        for status in ("queued", "dispatching", "running", "unknown"):
            record["status"] = status
            response = await self.client.post(path, headers=headers)
            self.assertEqual(await response.json(), {"idle": False})
            self.assertEqual(self.bridge.identity(), identity)
        record["status"] = "completed"
        # Also retain a result that the native Bridge has not delivered yet.
        self.bridge.jobs.add("undelivered-result")
        self.assertEqual(await (await self.client.post(path, headers=headers)).json(), {"idle": False})
        self.bridge.jobs.clear()
        # Consume the close frame so the server can finish its close handshake.
        receive = asyncio.create_task(self.mac.receive(timeout=2))
        self.assertEqual(await (await self.client.post(path, headers=headers)).json(), {"idle": True})
        await receive
        with self.assertRaises(ValueError):
            self.bridge.identity()
        # The retired connection cannot accept another graph during shutdown.
        self.assertEqual((await self.client.post("/prompt", json=body)).status, 409)
        self.assertEqual(len(self.controller.journal.data["jobs"]), 1)
        self.controller.worker.spawn.aio.assert_not_awaited()

    async def test_idle_does_not_cancel_unrelated_video_generation(self):
        await self.connect_mac()
        record = self.controller.journal.enqueue({"prompt": {"1": {"class_type": "HunyuanVideo"}}})
        record["status"] = "running"
        receive = asyncio.create_task(self.mac.receive(timeout=2))
        response = await self.client.post(PREFIX + "/idle", headers={"Authorization": "Bearer shared-test-token"})
        self.assertEqual(await response.json(), {"idle": True})
        await receive
        self.assertEqual(record["status"], "running")
        self.assertTrue(self.controller.background_work())


class WorkerBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_waits_for_attachment_and_ordinary_workflows_skip_tunnel(self):
        order = []

        @asynccontextmanager
        async def tunnel(*_, **kwargs):
            order.append("tunnel")
            yield {"type": "agent_bridge_ready"}
            order.append("closed")

        async def emit(_):
            order.append("ready")

        async def generate(*_):
            order.append("generate")
            return {"status": "completed"}

        with patch.dict(os.environ, {"SPLIT_AGENT_BRIDGE": "on"}), patch("comfyui_agent_bridge.split.gpu_tunnel", tunnel), patch("comfy_split.worker.generate", generate):
            await generate_with_extensions({}, None, None, None)
            self.assertEqual(order, ["generate"])
            order.clear()
            await generate_with_extensions({"agent_bridge": "mac"}, None,
                AsyncMock(return_value=[{"type": "agent_bridge_connected"}]), emit)
            self.assertEqual(order, ["tunnel", "ready", "generate", "closed"])


if __name__ == "__main__":
    unittest.main()
