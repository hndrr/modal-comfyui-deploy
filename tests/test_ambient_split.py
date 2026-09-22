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
        )
        self.control.cleanup_storage = AsyncMock()
        self.cancelled = False
        self.cancel_on_upload = False
        self.switch_on_catalog = False
        self.cpu_requests = []
        self.upload = None
        self.tasks = []
        self.queued = asyncio.Event()
        self.sampled = asyncio.Event()
        self.progress_events = []
        self.req = request()
        self.generation_timeout = 5

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

    def progress(self, stage, values=None):
        self.progress_events.append((stage, values))
        if stage == "Sampling":
            self.sampled.set()
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
            timeout=self.generation_timeout,
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

    async def test_catalog_upgrades_saved_length_and_accepts_new_split_recipes(self):
        from copy import deepcopy
        from test_split_generation import objects
        from comfy_split.generation import workflow, MODES
        from comfy_split.ambient_workflows import h3_metadata

        info = objects()
        req = request(mode="h3-turbo-4step", sessionId="00000000-0000-4000-8000-000000000000", workflowRevision=0)
        graph = workflow(req, object_info=info)
        meta = h3_metadata(req, graph)
        del meta["bindings"]["length"]
        registry = self.control.ambient_workflows
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

    async def test_sampling_progress_is_scoped_to_the_accepted_prompt(self):
        task, job = await self.start_generation()
        client_id = job["body"]["client_id"]
        for prompt_id, value in [("another-session", 1), (job["id"], -1), (job["id"], 4)]:
            await self.control.broadcast({"type": "progress", "data": {
                "prompt_id": prompt_id, "value": value, "max": 8}}, client_id)
        await asyncio.wait_for(self.sampled.wait(), 2)
        self.assertEqual([data for stage, data in self.progress_events if stage == "Sampling"],
                         [{"value": 4, "max": 8}])
        self.cancelled = True
        await asyncio.wait_for(task, 3)
        self.worker.spawn.aio.assert_not_awaited()

    async def test_workflow_bootstrap_apply_conflict_and_pinned_execution_without_gpu(self):
        from copy import deepcopy
        from ambient.h3 import workflow
        from comfy_split.ambient_workflows import h3_metadata
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
        from ambient.h3 import workflow
        from comfy_split.ambient_workflows import h3_metadata
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
        self.control.ambient_workflows.state["defaults"]["text"] = {"graph": {"old": "shipped template"}}
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

    async def test_eight_step_i2v_upload_dispatch_and_download(self):
        self.req = request(mode="fasth3-8step-i2v", backend="comfyui")
        record = await check_comfyui(self.base, {}, self.req["mode"])
        self.assertFalse(record["gpuValidated"])
        self.worker.spawn.aio.assert_not_awaited()
        await self.test_anchor_generation_dispatches_once_and_downloads_while_ui_stays_open()
        graph = next(iter(self.control.journal.data["jobs"].values()))["body"]["prompt"]
        self.assertEqual(graph["2"]["inputs"]["selection"], "sol-attn")
        self.assertEqual(graph["10"]["inputs"]["steps"], 8)

    async def test_eight_step_i2v_without_image_rejects_before_queueing(self):
        from uuid import uuid4
        self.req = request(mode="fasth3-8step-i2v", sessionId=str(uuid4()), workflowRevision=0)
        with self.assertRaisesRegex(RuntimeError, "requires a first-frame"):
            await self.generate()
        self.assertEqual(self.control.journal.data["jobs"], {})
        self.worker.spawn.aio.assert_not_awaited()

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

    async def test_deadline_cancels_only_own_queued_job(self):
        self.generation_timeout = 0
        other = self.control.journal.enqueue({"prompt": {"1": {}}})
        task, own = await self.start_generation()
        with self.assertRaisesRegex(TimeoutError, "cancellation requested"):
            await asyncio.wait_for(task, 3)
        self.assertEqual(own["status"], "cancelled")
        self.assertEqual(other["status"], "queued")
        self.commands.put.aio.assert_not_awaited()
        self.worker.spawn.aio.assert_not_awaited()

    async def test_deadline_sends_interrupt_only_to_own_running_job(self):
        self.generation_timeout = 0.05
        task, own = await self.start_generation()
        async with self.control.lock:
            await self.control.spawn(own, "generate")
            other = self.control.journal.enqueue({"prompt": {"1": {}}})
        with self.assertRaisesRegex(TimeoutError, "cancellation requested"):
            await asyncio.wait_for(task, 3)
        self.commands.put.aio.assert_awaited_once_with({"type": "interrupt"}, partition=own["id"])
        self.assertEqual(other["status"], "queued")
        self.assertNotIn("/interrupt", self.cpu_requests)

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
