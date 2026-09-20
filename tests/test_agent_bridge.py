import asyncio
import json
import io
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from comfy_split.agent_bridge import PREFIX, TOKEN_ENV, await_connection, gpu_tunnel
from comfy_split.bridge_transport import HttpSocket, TRANSPORT, MAX_MESSAGE
from comfy_split.gateway import Controller
from comfy_split.worker import generate_with_bridge


def remote(value=None):
    return SimpleNamespace(aio=AsyncMock(return_value=value))


HELLO = {"type": "hello", "version": 1, "worker_id": "mac-test", "catalog": {
    "models": ["test-model"], "skills": [], "revision": "test", "auth": "authenticated"}}


class NativeBridge:
    """Independent protocol peer: validates auth, catalogs and streamed files."""
    def __init__(self):
        self.socket = None
        self.hello = None
        self.messages = asyncio.Queue()
        self.upload = None

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
                    await self.messages.put(data)
            finally:
                self.socket = None
            return socket
        if request.path.endswith("/inputs/0"):
            return web.Response(body=b"input-image")
        if request.path.endswith("/artifacts"):
            self.upload = await request.read()
            return web.json_response({"id": "artifact-1", "size": len(self.upload)})
        raise web.HTTPNotFound()


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {TOKEN_ENV: "shared-test-token", "COMFYUI_AMBIENT_MODE": "on"})
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
        await self.controller.bridge.close()
        await self.client.close()
        await self.controller.client.close()
        for server in self.servers:
            await server.close()

    async def connect_mac(self):
        self.mac = await self.client.ws_connect(PREFIX + "/ws", headers={
            "Authorization": "Bearer shared-test-token"}, max_msg_size=20 * 1024 * 1024)
        await self.mac.send_json(HELLO)
        self.assertEqual(await self.mac.receive_json(timeout=2), {"type": "ready", "version": 1})

    async def attach_gpu(self):
        record = {"id": "workflow-1", "agent_bridge": self.controller.bridge.identity()}
        await self.controller.bridge.attach(record, {"url": self.gpu_url, "token": "shared-test-token"})
        return record

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
        self.assertEqual(job["agent_bridge"], self.controller.bridge.identity())
        self.controller.bridge.connection_id = "different-connection"
        await self.controller.spawn(job, "generate")
        self.assertEqual(job["status"], "failed")
        self.controller.worker.spawn.aio.assert_not_awaited()
        self.controller.bridge.connection_id = None
        retry = await (await self.client.post("/prompt", json=body, headers=headers)).json()
        self.assertEqual(retry["prompt_id"], job["id"])
        self.assertEqual(len(self.controller.journal.data["jobs"]), 1)

    async def test_idle_disconnect_requires_auth_and_preserves_accepted_bridge_work(self):
        path = PREFIX + "/idle"
        headers = {"Authorization": "Bearer shared-test-token"}
        self.assertEqual((await self.client.post(path)).status, 401)
        await self.connect_mac()
        identity = self.controller.bridge.identity()
        body = {"prompt": {"1": {"class_type": "AgentRuntimeBridgeText"}}}
        accepted = await (await self.client.post("/prompt", json=body)).json()
        record = self.controller.journal.data["jobs"][accepted["prompt_id"]]
        for status in ("queued", "dispatching", "running", "unknown"):
            record["status"] = status
            response = await self.client.post(path, headers=headers)
            self.assertEqual(await response.json(), {"idle": False})
            self.assertEqual(self.controller.bridge.identity(), identity)
        record["status"] = "completed"
        # Also retain a result that the native Bridge has not delivered yet.
        self.controller.bridge.jobs.add("undelivered-result")
        self.assertEqual(await (await self.client.post(path, headers=headers)).json(), {"idle": False})
        self.controller.bridge.jobs.clear()
        # Consume the close frame so the server can finish its close handshake.
        receive = asyncio.create_task(self.mac.receive(timeout=2))
        self.assertEqual(await (await self.client.post(path, headers=headers)).json(), {"idle": True})
        await receive
        with self.assertRaises(ValueError):
            self.controller.bridge.identity()
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

    async def test_files_large_results_events_catalogs_and_cleanup(self):
        await self.connect_mac()
        record = await self.attach_gpu()
        self.assertEqual(self.gpu.hello["worker_id"], "mac-test")
        await self.gpu.socket.send_json({"type": "job", "id": "native-job", "spec": {"kind": "text"}})
        self.assertEqual((await self.mac.receive_json(timeout=2))["id"], "native-job")
        headers = {"Authorization": "Bearer shared-test-token"}
        base = "/api" + PREFIX + "/jobs/native-job"
        self.assertEqual((await self.client.get(base + "/inputs/0")).status, 401)
        self.assertEqual(await (await self.client.get(base + "/inputs/0", headers=headers)).read(), b"input-image")
        image = b"image-result" * 150000
        response = await self.client.post(base + "/artifacts?name=image.png", data=io.BytesIO(image), headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.gpu.upload, image)
        await self.mac.send_json({"type": "event", "id": "native-job", "event": {"progress": 20}})
        result = {"text": "x" * (2 * 1024 * 1024), "artifacts": ["artifact-1"]}
        await self.mac.send_json({"type": "result", "id": "native-job", "result": result})
        async with asyncio.timeout(3):
            received = []
            while not received or received[-1]["type"] != "result":
                received.append(await self.gpu.messages.get())
        self.assertEqual(received[-1]["result"], result)
        self.assertTrue(any(item["type"] == "event" for item in received))
        self.assertEqual((await self.client.get(base + "/inputs/0", headers=headers)).status, 404)
        catalog = dict(HELLO["catalog"], revision="updated")
        await self.mac.send_json({"type": "catalog", "catalog": catalog})
        self.assertEqual((await asyncio.wait_for(self.gpu.messages.get(), 2))["catalog"], catalog)
        await self.controller.bridge.detach(record["id"])
        self.assertFalse(self.mac.closed)
        self.assertIsNone(self.controller.bridge.url)

    async def test_gpu_disconnect_cancels_mac_job_without_replay(self):
        await self.connect_mac()
        record = await self.attach_gpu()
        await self.gpu.socket.send_json({"type": "job", "id": "native-job"})
        await self.mac.receive_json(timeout=2)
        await self.gpu.socket.close()
        self.assertEqual(await self.mac.receive_json(timeout=2), {"type": "cancel", "id": "native-job"})
        with self.assertRaises(ValueError):
            await self.controller.bridge.attach(record, {"url": self.gpu_url, "token": "shared-test-token"})

    async def test_mac_disconnect_closes_gpu(self):
        await self.connect_mac()
        await self.attach_gpu()
        await self.mac.close()
        async with asyncio.timeout(2):
            while self.gpu.socket is not None:
                await asyncio.sleep(0.01)
        self.assertIsNone(self.controller.bridge.connection_id)

    async def test_tunnel_auth_scope_and_shutdown(self):
        @asynccontextmanager
        async def forward(port):
            yield SimpleNamespace(url=f"http://127.0.0.1:{port}")
        with patch("comfy_split.agent_bridge.modal.forward", forward):
            async with gpu_tunnel(self.controller.client, self.gpu_url) as ready:
                url = ready["url"]
                headers = {"Authorization": "Bearer " + ready["token"]}
                response = await self.controller.client.get(url + PREFIX + "/ws")
                self.assertEqual(response.status, 401)
                response = await self.controller.client.get(url + "/object_info", headers=headers)
                self.assertEqual(response.status, 404)
                socket = await self.controller.client.ws_connect(url + PREFIX + "/ws", headers=headers)
                await socket.send_json(HELLO)
                self.assertEqual((await socket.receive_json())["type"], "ready")
            await socket.receive(timeout=2)
            self.assertTrue(socket.closed)

    async def test_http_payloads_cross_both_gateway_and_gpu_tunnel(self):
        self.mac = await self.client.ws_connect(PREFIX + "/ws", headers={
            "Authorization": "Bearer shared-test-token"}, max_msg_size=2 * 1024 * 1024)
        await self.mac.send_json(dict(HELLO, transport=TRANSPORT))
        ready = await self.mac.receive_json(timeout=2)
        self.assertEqual(ready["transport"], TRANSPORT)
        public_url = str(self.client.make_url("/")).rstrip("/")
        peer = HttpSocket(self.mac, self.controller.client, public_url, "shared-test-token", ready["session"])

        @asynccontextmanager
        async def forward(port):
            yield SimpleNamespace(url=f"http://127.0.0.1:{port}")

        with patch("comfy_split.agent_bridge.modal.forward", forward):
            async with gpu_tunnel(self.controller.client, self.gpu_url) as endpoint:
                record = {"id": "large-workflow", "agent_bridge": self.controller.bridge.identity()}
                await self.controller.bridge.attach(record, endpoint)
                prompt = "p" * (3 * 1024 * 1024)
                await self.gpu.socket.send_json({"type": "job", "id": "large-job", "spec": {"prompt": prompt}})
                async with asyncio.timeout(5):
                    message = await anext(aiter(peer))
                self.assertEqual(json.loads(message.data)["spec"]["prompt"], prompt)
                result = {"response": "r" * (6 * 1024 * 1024)}
                await peer.send_json({"type": "result", "id": "large-job", "result": result})
                async with asyncio.timeout(5):
                    while True:
                        received = await self.gpu.messages.get()
                        if received["type"] == "result":
                            break
                self.assertEqual(received["result"], result)
                self.assertFalse(self.controller.bridge.channel.incoming)
                self.assertFalse(self.controller.bridge.channel.outgoing)
                await self.controller.bridge.detach(record["id"])
        await self.mac.close()
        headers = {"Authorization": "Bearer shared-test-token"}
        response = await self.client.post(PREFIX + "/messages", params={"session": ready["session"]},
                                          headers=headers, data="{}")
        self.assertEqual(response.status, 409)

    async def test_http_payload_auth_limit_and_replayed_reference(self):
        self.mac = await self.client.ws_connect(PREFIX + "/ws", headers={
            "Authorization": "Bearer shared-test-token"})
        await self.mac.send_json(dict(HELLO, transport=TRANSPORT))
        ready = await self.mac.receive_json(timeout=2)
        params = {"session": ready["session"]}
        headers = {"Authorization": "Bearer shared-test-token"}
        route = PREFIX + "/messages"
        self.assertEqual((await self.client.post(route, params=params, data="{}")).status, 401)
        response = await self.client.post(route, params=params, headers=headers,
                                          data=io.BytesIO(b"x" * (MAX_MESSAGE + 1)))
        self.assertEqual(response.status, 413)
        response = await self.client.post(route, params=params, headers=headers,
            data=json.dumps({"type": "catalog", "catalog": HELLO["catalog"]}))
        key = (await response.json())["id"]
        reference = {"type": "bridge_payload", "id": key}
        await self.mac.send_json(reference)
        await self.mac.send_json(reference)
        await self.mac.receive(timeout=2)
        self.assertTrue(self.mac.closed)


class WorkerBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_waits_for_attachment_and_ordinary_workflows_skip_tunnel(self):
        order = []

        @asynccontextmanager
        async def tunnel(*_):
            order.append("tunnel")
            yield {"type": "agent_bridge_ready"}
            order.append("closed")

        async def emit(_):
            order.append("ready")

        async def generate(*_):
            order.append("generate")
            return {"status": "completed"}

        with patch("comfy_split.worker.gpu_tunnel", tunnel), patch("comfy_split.worker.generate", generate):
            await generate_with_bridge({}, None, None, None)
            self.assertEqual(order, ["generate"])
            order.clear()
            await generate_with_bridge({"agent_bridge": "mac"}, None,
                AsyncMock(return_value=[{"type": "agent_bridge_connected"}]), emit)
            self.assertEqual(order, ["tunnel", "ready", "generate", "closed"])

    async def test_interrupt_wins_over_ready_and_attach_failure_is_terminal(self):
        self.assertFalse(await await_connection({}, AsyncMock(return_value=[
            {"type": "agent_bridge_connected"}, {"type": "interrupt"}]), AsyncMock()))
        with self.assertRaises(RuntimeError):
            await await_connection({}, AsyncMock(return_value=[{"type": "agent_bridge_error"}]), AsyncMock())


if __name__ == "__main__":
    unittest.main()
