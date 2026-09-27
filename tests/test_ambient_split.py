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

from ambient_fixtures import object_info, request, workflow
from comfy_split.gateway import Controller
from ambient_comfyui.split import SplitAmbient


def remote(result=None):
    return SimpleNamespace(aio=AsyncMock(return_value=result))


SPLIT_HEADERS = {"X-Modal-Execution-Mode": "split"}

class SplitIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.worker = SimpleNamespace(
            spawn=remote(SimpleNamespace(object_id="fc-test")),
            get_current_stats=remote(SimpleNamespace(num_total_runners=0)),
        )
        self.commands = SimpleNamespace(put=remote())
        self.volumes = {
            name: SimpleNamespace(commit=remote(), reload=remote())
            for name in ("input", "output", "data", "models", "environment")
        }
        async def missing_file(_):
            raise FileNotFoundError
            yield b""
        self.volumes["data"].read_file = SimpleNamespace(aio=missing_file)
        self.ui = SimpleNamespace(update_autoscaler=remote())
        self.control = Controller(
            self.worker,
            SimpleNamespace(get_many=remote([])),
            self.commands,
            self.volumes,
            self.root,
            ui_function=self.ui,
            extensions=[SplitAmbient],
        )
        self.control.cleanup_storage = AsyncMock()
        self.switch_on_catalog = False
        self.cpu_requests = []
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
                form["image"].file.close()
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
        await self.client.close()
        await self.control.client.close()
        await self.cpu_server.close()

    async def test_saved_media_is_read_through_optional_adapter_without_old_app(self):
        from copy import deepcopy
        from uuid import uuid4
        from ambient_comfyui import saved_library

        identity = str(uuid4())
        records = {"clip:" + identity: {"id": identity, "createdAt": 1, "bytes": 6}}
        before = deepcopy(records)
        async def content(path):
            self.assertEqual(path, f"ambient/library/{identity}.mp4")
            yield b"oldmp4"
        self.volumes["output"].read_file = SimpleNamespace(aio=content)
        with patch.object(saved_library, "read_records", AsyncMock(return_value=records)):
            response = await self.client.get("/ambient/library")
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["count"], 1)
            response = await self.client.get(f"/ambient/library/{identity}/video")
            self.assertEqual(await response.read(), b"oldmp4")
            self.assertEqual((await self.client.delete("/ambient/library")).status, 405)
        self.assertEqual(records, before)
        self.worker.spawn.aio.assert_not_awaited()
        self.volumes["output"].commit.aio.assert_not_awaited()


    async def test_catalog_upgrades_saved_length_and_accepts_new_split_recipes(self):
        from copy import deepcopy
        from test_split_generation import objects, workflow, MODES
        from ambient_comfyui.workflows import h3_metadata

        info = objects()
        req = request(mode="h3-turbo-4step", sessionId="00000000-0000-4000-8000-000000000000", workflowRevision=0)
        graph = workflow(req, object_info=info)
        meta = h3_metadata(req, graph)
        del meta["bindings"]["length"]
        registry = self.control.plugins[0].registry
        registry.prepare({"prompt": graph, "extra_data": {"ambient": meta}})
        old = deepcopy(registry.describe()["stages"][req["mode"]])
        old["graph"]["4"]["inputs"]["vae_name"] = "custom-video-vae.safetensors"
        info["VAELoader"]["input"]["required"]["vae_name"][0].append("custom-video-vae.safetensors")
        registry.apply(req["mode"], old, 0, info)
        with patch("test_ambient_split.object_info", return_value=info):
            response = await self.client.get("/ambient/workflows")
            self.assertEqual(response.status, 200)
            catalog = await response.json()
            self.assertTrue(set(MODES) <= set(catalog["stages"]))
            self.assertEqual(catalog["revision"], 2)
            upgraded = catalog["stages"][req["mode"]]
            self.assertEqual(upgraded["bindings"]["length"]["source"], "ambient")
            self.assertEqual(upgraded["graph"], old["graph"])
            self.assertNotIn("length", registry.snapshot(1)["stages"][req["mode"]]["bindings"])
            req.update(mode="h3-ref2v", workflowRevision=2, frames=243, referenceNames=["b.png", "a.png"])
            graph = workflow(req, object_info=info)
            response = await self.client.post("/prompt", json={"prompt": graph,
                "extra_data": {"ambient": h3_metadata(req, graph)}})
            self.assertEqual(response.status, 200, await response.text())
            job = self.control.journal.data["jobs"][(await response.json())["prompt_id"]]
            self.assertEqual(job["body"]["prompt"]["6"]["inputs"]["length"], 243)
            self.assertEqual(job["body"]["prompt"]["ambient_ref_0"]["inputs"]["image"], "b.png")
        self.worker.spawn.aio.assert_not_awaited()


    async def test_workflow_bootstrap_apply_conflict_and_pinned_execution_without_gpu(self):
        from copy import deepcopy
        from ambient_comfyui.workflows import h3_metadata
        from uuid import uuid4

        response = await self.client.get("/ambient/workflows")
        self.assertEqual(response.status, 200)
        catalog = await response.json()
        self.assertEqual(catalog["revision"], 0)
        self.assertIn("h3", catalog["stages"])
        self.assertIn("fasth3-8step-t2v", catalog["stages"])
        self.assertIn("fasth3-8step-i2v", catalog["stages"])
        self.assertIn("h3-turbo-4step", catalog["stages"])
        self.assertIn("h3-fused-4step", catalog["stages"])
        template = deepcopy(catalog["stages"]["h3"])
        template["bindings"]["seed"]["source"] = "workflow"
        template["graph"]["8"]["inputs"]["noise_seed"] = 123
        payload = {"stage": "h3", "template": template, "expectedRevision": 0}
        self.assertEqual((await self.client.post("/ambient/workflows/validate", json=payload)).status, 200)
        applied = await self.client.post("/ambient/workflows/apply", json=payload)
        self.assertEqual(applied.status, 200)
        self.assertEqual((await applied.json())["revision"], 1)
        self.assertEqual((await self.client.post("/ambient/workflows/apply", json=payload)).status, 409)
        for revision, seed in [(0, 42), (1, 123)]:
            req = {**self.req, "sessionId": str(uuid4()), "workflowRevision": revision}
            graph = workflow(req, object_info=object_info())
            body = {"prompt": graph, "client_id": "ambient-client", "extra_data": {"ambient": h3_metadata(req, graph)}}
            submitted = await (await self.client.post("/prompt", json=body)).json()
            job = self.control.journal.data["jobs"][submitted["prompt_id"]]
            self.assertEqual(job["body"]["prompt"]["8"]["inputs"]["noise_seed"], seed)
        self.worker.spawn.aio.assert_not_awaited()


    async def test_mirror_events_and_reconnect_snapshot_are_session_scoped(self):
        from ambient_comfyui.workflows import h3_metadata
        from uuid import uuid4
        req = {**self.req, "sessionId": str(uuid4()), "workflowRevision": 0}
        graph = workflow(req, object_info=object_info())
        body = {"prompt": graph, "client_id": "generator", "extra_data": {"ambient": h3_metadata(req, graph)}}
        job_id = (await (await self.client.post("/prompt", json=body)).json())["prompt_id"]
        async with self.client.ws_connect("/ws?clientId=mirror") as socket:
            await socket.receive_json()
            event = {"type": "executing", "data": {"prompt_id": job_id, "node": "11"}}
            await self.control.broadcast(event, "generator")
            message = await socket.receive_json()
            self.assertEqual(message["type"], "ambient_execution")
            self.assertEqual(message["data"]["sessionId"], req["sessionId"])
        snapshot = await (await self.client.get("/ambient/executions", params={"sessionId": req["sessionId"]})).json()
        self.assertEqual(snapshot["executions"][0]["event"], event)
        other = await (await self.client.get("/ambient/executions", params={"sessionId": str(uuid4())})).json()
        self.assertEqual(other["executions"], [])
        self.worker.spawn.aio.assert_not_awaited()


    async def test_bridge_templates_are_available_before_any_execution(self):
        self.control.plugins[0].registry.state["defaults"]["text"] = {"graph": {"old": "shipped template"}}
        objects = {**object_info(), **{kind: {} for kind in (
            "AgentRuntimeBridgeText", "AgentRuntimeBridgeMedia", "AgentRuntimeBridgeImageGen", "PreviewAny", "PreviewImage")}}
        with patch.object(self.control, "objects", AsyncMock(return_value=web.json_response(objects))):
            catalog = await (await self.client.get("/ambient/workflows")).json()
        self.assertTrue({"media", "text", "imagegen"} <= set(catalog["stages"]))
        self.assertEqual(catalog["stages"]["text"]["graph"]["1"]["inputs"]["sandbox_mode"], "read-only")
        self.assertEqual(self.control.journal.data["jobs"], {})
        initial = await (await self.client.get("/ambient/workflows/0")).json()
        self.assertEqual(initial["stages"]["text"], catalog["stages"]["text"])
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
