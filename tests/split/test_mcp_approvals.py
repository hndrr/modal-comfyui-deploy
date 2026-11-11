import asyncio
import copy
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from comfy_split import gpu
from comfy_split import mcp_approvals as approvals
from comfy_split import mcp_policy as policy
from comfy_split.gateway import Controller
from comfy_split.state import Journal
from comfy_split.storage import MOUNTS
from tests.split.support import remote_mock, volume_mocks

LOCAL = {"1": {"class_type": "EmptyImage", "inputs": {}}}
PAID = {
    "1": {"class_type": "GeminiNode", "inputs": {"prompt": "雨の夜 <script>", "model": "test-model"}}
}
DEFS = {
    "EmptyImage": {"python_module": "nodes"},
    "LoadImage": {"python_module": "nodes"},
    "GeminiNode": {"python_module": "comfy_api_nodes.nodes_gemini", "api_node": True},
    "OpenAIChatNode": {"python_module": "comfy_api_nodes.nodes_openai", "api_node": True},
    "OpenRouterLLMNode": {"python_module": "comfy_api_nodes.nodes_openrouter", "api_node": True},
    "OpenRouterText": {"python_module": "custom_nodes.ComfyUI-Jev"},
    "ComfyCloudMiniMaxMusic3TextToAudioNode": {
        "python_module": "comfy_api_nodes.nodes_comfy_cloud",
        "api_node": True,
    },
    "MiniMaxMusic3TextEncode": {"python_module": "comfy_extras.nodes_minimax_music"},
}


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Simulate a verified API entry to test precedence and pinned approval.
        self.enterContext(patch.dict(gpu.CPU_API_NODES, {"comfy_api_nodes.nodes_gemini": {"GeminiNode"}}))
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.input = root / "input"
        self.input.mkdir()
        self.enterContext(patch.dict(MOUNTS, input=str(self.input)))
        self.enterContext(patch.dict("os.environ", SPLIT_COMFY_BILLING_ENABLED="1"))
        self.control = Controller(
            SimpleNamespace(spawn=remote_mock(),
                            get_current_stats=remote_mock(SimpleNamespace(num_total_runners=0, backlog=0))),
            None, None, volume_mocks(), root, extensions=[]
        )
        self.control.objects = AsyncMock(return_value=web.json_response(DEFS))
        app = web.Application()
        app.router.add_route("*", "/{rest:.*}", self.control.handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.review_headers = {"X-Split-Browser-Review": "1"}

    async def send(self, key, graph, **kwargs):
        return await self.client.post(
            "/_remote_mcp/submit",
            headers={
                "X-Split-Remote-Tool": "1",
                "X-Modal-Execution-Mode": "split",
                "Idempotency-Key": key,
            },
            json={"prompt": graph, **kwargs},
        )

    async def pending(self, key, graph, **kwargs):
        r = await self.send(key, graph, **kwargs)
        self.assertEqual(r.status, 202, await r.text())
        receipt = await r.json()
        path = receipt["approval_path"].replace(approvals.PUBLIC, approvals.INTERNAL)
        self.assertNotIn("csrf", receipt)
        page = await self.client.get(path, headers=self.review_headers)
        # Keep the browser form compatible with the Worker's exact Origin check
        # while withholding the review URL from cross-origin destinations.
        self.assertEqual(page.headers["Referrer-Policy"], "same-origin")
        html = await page.text()
        token = re.search("name=csrf value='([^']+)'", html)[1]
        self.assertNotIn(token, str(receipt))
        return path, token, html

    async def decide(self, path, csrf, decision):
        return await self.client.post(
            path, headers=self.review_headers, data={"csrf": csrf, "decision": decision}
        )

    async def test_local_immediate_and_unknown_provenance_rejected(self):
        self.assertEqual((await self.send("render", LOCAL)).status, 200)
        receipt = await (await self.send("render", LOCAL)).json()
        self.assertEqual(list(self.control.journal.data["jobs"]), [receipt["prompt_id"]])
        disguised = {"1": {"class_type": "EmptyImage", "inputs": {}, "python_module": "nodes"}}
        self.control.objects.return_value = web.json_response(
            {"EmptyImage": {}}
        )
        self.assertEqual((await self.send("spoof", disguised)).status, 409)

    async def test_installed_custom_nodes_reach_gpu_without_mcp_registration(self):
        from unittest.mock import MagicMock
        from comfy_split import worker

        for name, module in (("HyperFlowLoader", "custom_nodes.comfyui-hyperflow-flash"),
                             ("NewLocalNode", "custom_nodes.new-pack"),
                             ("ComfySkillsLoader", "custom_nodes.ComfyUI-Skills-Loader"),
                             ("NewCoreNode", "comfy_extras.nodes_new")):
            with self.subTest(name=name):
                graph = {"1": {"class_type": name, "inputs": {}}}
                definitions = {name: {"python_module": module}}
                self.control.objects.return_value = web.json_response(definitions)
                for choice, expected in (("auto", "RTX-PRO-6000"), ("auto-speed", "RTX-PRO-6000"),
                                         ("L40S", "L40S")):
                    key = name + choice
                    response = await self.send(key, graph, gpu=choice)
                    self.assertEqual(response.status, 200, await response.text())
                    receipt = await response.json()
                    self.assertEqual(receipt["gpu"], expected)
                    self.assertEqual(await (await self.send(key, graph, gpu=choice)).json(), receipt)
                job = self.control.journal.data["jobs"][receipt["prompt_id"]]
                self.assertEqual(job["mcp_guard"]["routes"], [])
                client = MagicMock()
                response = MagicMock()
                response.json = AsyncMock(return_value=definitions)
                client.get.return_value.__aenter__.return_value = response
                with patch.object(worker, "generate", new_callable=AsyncMock) as run:
                    await worker.generate_with_extensions(
                        {"id": receipt["prompt_id"], "body": job["body"], "mcp_guard": job["mcp_guard"]},
                        client, None, None)
                    run.assert_awaited_once()
        self.assertFalse(self.control.journal.data["mcp_reviews"])

    async def test_custom_node_fallback_preserves_external_api_boundaries(self):
        for i, definition in enumerate((
            {"python_module": "custom_nodes.new", "api_node": True},
            {"python_module": "comfy_api_nodes.nodes_new", "api_node": True},
            {"python_module": "comfy_api_nodes.nodes_gemini"},
            {"python_module": "custom_nodes.comfyui-geminitools"},
            {"python_module": "custom_nodes.comfyui-jev"},
            {"python_module": "custom_nodes.ComfyUI-AgentRuntime"},
            {"python_module": "custom_nodes.ComfyUI-AgentBridge"},
        )):
            self.control.objects.return_value = web.json_response({"NewAPI": definition})
            graph = {"1": {"class_type": "NewAPI", "inputs": {}, "api_node": False,
                           "python_module": "custom_nodes.local"}}
            denied = await self.send("unknown-api-" + str(i), graph)
            self.assertEqual(denied.status, 409, await denied.text())
            self.assertIn("外部への送信内容", (await denied.json())["error"])
        self.assertFalse(self.control.journal.data["jobs"])

        for name, module in (("GeminiToolsLLM", "custom_nodes.comfyui-geminitools"),
                             ("OpenRouterText", "custom_nodes.comfyui-jev")):
            self.control.objects.return_value = web.json_response({
                name: {"python_module": module},
                "LocalPostProcess": {"python_module": "custom_nodes.new-pack"},
            })
            graph = {"1": {"class_type": name, "inputs": {"prompt": "hello"}},
                     "2": {"class_type": "LocalPostProcess", "inputs": {"input": ["1", 0]}}}
            _, _, html = await self.pending("direct-" + name, graph)
            self.assertIn("RTX-PRO-6000", html)
            self.assertIn("hello", html)
            self.assertFalse(self.control.journal.data["jobs"])

    async def test_unregistered_api_still_needs_approval_for_gpu_or_cpu_trial(self):
        with patch.dict(gpu.CPU_API_NODES, {}, clear=True):
            for key, choice, expected in (("auto-api", "auto", "L4"),
                                          ("speed-api", "auto-speed", "L4"),
                                          ("manual-api", "L40S", "L40S"),
                                          ("trial-api", "CPU", "CPU")):
                path, csrf, html = await self.pending(key, PAID, gpu=choice)
                self.assertIn(f"実行先：<strong>{expected}", html)
                self.assertFalse(self.control.journal.data["jobs"])
            for choice in ("auto", "auto-speed"):
                receipt = await (await self.client.post("/api/prompt", json={"prompt": PAID, "gpu": choice})).json()
                self.assertEqual(receipt["gpu"], "L4")
            path, csrf, _ = await self.pending("auto-api", PAID)
            await self.decide(path, csrf, "approve")
            receipt = await (await self.send("auto-api", PAID)).json()
            self.assertEqual(receipt["gpu"], "L4")

    async def test_gpu_selection_is_pinned_across_browser_mcp_review_and_replay(self):
        browser = await (await self.client.post("/api/prompt", json={"prompt": PAID})).json()
        automatic = await (await self.send("auto", PAID)).json()
        self.assertEqual(browser["gpu"], "CPU")
        self.assertEqual(browser["gpu_reason"], automatic["gpu_reason"])
        self.assertEqual(automatic["gpu"], browser["gpu"])
        path, csrf, html = await self.pending("fixed", PAID, gpu="L40S")
        self.assertIn("実行先：<strong>CPU", html)
        self.assertEqual((await self.send("fixed", PAID, gpu="CPU")).status, 409)
        await self.decide(path, csrf, "approve")
        receipt = await (await self.send("fixed", PAID, gpu="L40S")).json()
        self.assertEqual(receipt["gpu"], "CPU")
        progress = await self.client.post("/_remote_mcp/progress", json={"prompt_id": receipt["prompt_id"]},
            headers={"X-Split-Remote-Tool": "1", "X-Modal-Execution-Mode": "split"})
        self.assertEqual((await progress.json())["gpu_reason"], receipt["gpu_reason"])
        self.control.journal.data["mcp_reviews"].clear()
        self.control.journal.data["jobs"][receipt["prompt_id"]]["status"] = "completed"
        self.control.journal.retire([receipt["prompt_id"]])
        self.assertEqual(await (await self.send("fixed", PAID, gpu="L40S")).json(), receipt)
        self.assertEqual((await self.send("fixed", PAID)).status, 409)

    async def test_auto_considers_local_work_after_external_generation(self):
        graph = {**PAID, "2": {"class_type": "ImageUpscaleWithModel", "inputs": {"image": ["1", 0]}}}
        self.control.objects.return_value = web.json_response({**DEFS, "ImageUpscaleWithModel":
            {"python_module": "comfy_extras.nodes_upscale_model", "output_node": True}})
        r = await self.client.post("/api/prompt", json={"prompt": graph})
        self.assertEqual((await r.json())["gpu"], "RTX-PRO-6000")
        r = await self.client.post("/api/prompt", json={"prompt": graph}, headers={"Cookie": "split_gpu=L40S"})
        self.assertEqual((await r.json())["gpu"], "L40S")

    async def test_explicit_cpu_bypasses_only_execution_eligibility(self):
        # Recognized local nodes may be tried on CPU, even without CPU registration.
        graph = {"1": {"class_type": "ImageUpscaleWithModel", "inputs": {}}}
        self.control.objects.return_value = web.json_response({"ImageUpscaleWithModel":
            {"python_module": "comfy_extras.nodes_upscale_model"}})
        r = await self.send("try-cpu", graph, gpu="CPU")
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual((await r.json())["gpu"], "CPU")
        self.control.objects.return_value = web.json_response(DEFS)
        path, csrf, html = await self.pending("cpu-api", PAID, gpu="CPU")
        self.assertIn("実行先：<strong>CPU", html)
        self.assertEqual(len(self.control.journal.data["jobs"]), 1)
        await self.decide(path, csrf, "approve")
        self.assertEqual((await (await self.send("cpu-api", PAID, gpu="CPU")).json())["gpu"], "CPU")
        self.control.objects.return_value = web.json_response({"NewAPINode":
            {"python_module": "custom_nodes.new", "api_node": True}})
        denied = await self.send("unsupported-cpu", {"1": {"class_type": "NewAPINode", "inputs": {}}}, gpu="CPU")
        self.assertEqual(denied.status, 409)
        self.assertEqual(len(self.control.journal.data["jobs"]), 2)

    async def test_cost_and_speed_priorities_match_ui_and_survive_approval_and_replay(self):
        fixture = Path(__file__).resolve().parents[2] / "scripts/verify_split_gpu_workflow.json"
        graph = json.loads(fixture.read_text())
        defs = {n["class_type"]: {"python_module": "nodes", "output_node": n["class_type"] == "SaveImage"}
                for n in graph.values()}
        defs["EmptyFlux2LatentImage"]["python_module"] = "comfy_extras.nodes_flux"
        self.control.objects.return_value = web.json_response(defs)
        for choice, device, label in [("auto", "L4", "料金優先"), ("auto-speed", "RTX-PRO-6000", "速度優先")]:
            browser = await (await self.client.post("/api/prompt", json={"prompt": graph},
                headers={"Cookie": f"split_gpu={choice}"})).json()
            receipt = await (await self.send(choice, graph, gpu=choice)).json()
            self.assertEqual(receipt["gpu"], device)
            self.assertEqual(browser["gpu"], device)
            self.assertEqual(browser["gpu_reason"], receipt["gpu_reason"])
            self.assertIn(label, receipt["gpu_reason"])
            self.assertEqual(await (await self.send(choice, graph, gpu=choice)).json(), receipt)
        self.assertEqual((await self.send("auto-speed", graph, gpu="auto")).status, 409)
        manual = await (await self.client.post("/api/prompt", json={"prompt": graph, "gpu": "L40S"},
            headers={"Cookie": "split_gpu=auto-speed"})).json()
        self.assertEqual(manual["gpu"], "L40S")

        self.control.objects.return_value = web.json_response(DEFS)
        path, csrf, html = await self.pending("speed-review", PAID, gpu="auto-speed")
        self.assertIn("実行先：<strong>CPU", html)
        self.assertIn("CPU", html)
        browser = await (await self.client.post("/api/prompt", json={"prompt": PAID},
            headers={"Cookie": "split_gpu=auto-speed"})).json()
        self.assertEqual(browser["gpu"], "CPU")
        self.assertEqual((await self.send("speed-review", PAID, gpu="auto")).status, 409)
        await self.decide(path, csrf, "approve")
        receipt = await (await self.send("speed-review", PAID, gpu="auto-speed")).json()
        self.assertEqual(receipt["gpu"], "CPU")
        self.assertIn("CPU", receipt["gpu_reason"])
        self.assertEqual(await (await self.send("speed-review", PAID, gpu="auto-speed")).json(), receipt)

    async def test_speed_reuses_warm_gpu_even_for_cpu_eligible_api_and_io(self):
        stats = self.control.worker.get_current_stats.aio
        stats.return_value = SimpleNamespace(num_total_runners=1, backlog=0)
        browser = await (await self.client.post("/api/prompt", json={"prompt": PAID},
            headers={"Cookie": "split_gpu=auto-speed"})).json()
        self.assertEqual(browser["gpu"], gpu.DEFAULT)
        self.assertIn("起動済みGPU", browser["gpu_reason"])
        light = {"1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1}}}
        immediate = await (await self.send("warm-io", light, gpu="auto-speed")).json()
        self.assertEqual(immediate["gpu"], gpu.DEFAULT)
        self.assertEqual(await (await self.send("warm-io", light, gpu="auto-speed")).json(), immediate)
        stats.reset_mock()
        for choice in ("auto", "CPU", "L40S"):
            self.assertEqual((await (await self.send(choice, light, gpu=choice)).json())["gpu"], "CPU")
        stats.assert_not_awaited()
        stats.return_value = SimpleNamespace(num_total_runners=0, backlog=0)
        self.assertEqual((await (await self.send("cold-io", light, gpu="auto-speed")).json())["gpu"], "CPU")
        stats.side_effect = TimeoutError("unavailable")
        with self.assertLogs("comfy_split.gateway", level="WARNING"):
            fallback = await (await self.send("stats-down", light, gpu="auto-speed")).json()
        self.assertEqual(fallback["gpu"], "CPU")
        self.control.worker.spawn.aio.assert_not_awaited()

    async def test_unregistered_api_reuses_warm_gpu_only_without_approval_wait(self):
        stats = self.control.worker.get_current_stats.aio
        stats.return_value = SimpleNamespace(num_total_runners=1, backlog=0)
        with patch.dict(gpu.CPU_API_NODES, {}, clear=True):
            browser = await (await self.client.post("/api/prompt", json={"prompt": PAID, "gpu": "auto-speed"})).json()
            self.assertEqual(browser["gpu"], gpu.DEFAULT)
            self.assertIn("起動済みGPU", browser["gpu_reason"])
            self.assertIn("CPU対応未登録", browser["gpu_reason"])
            stats.reset_mock()
            path, csrf, html = await self.pending("pending-unregistered", PAID, gpu="auto-speed")
            self.assertIn("実行先：<strong>L4", html)
            stats.assert_not_awaited()
            stats.return_value = SimpleNamespace(num_total_runners=0, backlog=0)
            await self.decide(path, csrf, "approve")
            receipt = await (await self.send("pending-unregistered", PAID, gpu="auto-speed")).json()
            self.assertEqual(receipt["gpu"], "L4")
            stats.assert_not_awaited()
            browser = await (await self.client.post("/api/prompt", json={"prompt": PAID, "gpu": "auto-speed"})).json()
            self.assertEqual(browser["gpu"], "L4")
            stats.side_effect = TimeoutError("unavailable")
            with self.assertLogs("comfy_split.gateway", level="WARNING"):
                browser = await (await self.client.post("/api/prompt", json={"prompt": PAID, "gpu": "auto-speed"})).json()
            self.assertEqual(browser["gpu"], "L4")

    async def test_speed_external_approval_ignores_warm_gpu_and_preserves_graph_choice(self):
        stats = self.control.worker.get_current_stats.aio
        stats.return_value = SimpleNamespace(num_total_runners=1, backlog=0)
        path, csrf, html = await self.pending("warm-api", PAID, gpu="auto-speed")
        self.assertIn("CPU", html)
        self.assertNotIn("起動済み", html)
        stats.assert_not_awaited()
        self.assertEqual((await (await self.send("cost-api", PAID)).json())["gpu"], "CPU")
        self.assertEqual((await (await self.send("manual-api", PAID, gpu="L40S")).json())["gpu"], "CPU")
        heavy = {**PAID, "2": {"class_type": "EmptyImage", "inputs": {"width": 8192, "height": 8192, "batch_size": 1}}}
        self.assertEqual((await (await self.send("local-after-api", heavy, gpu="auto-speed")).json())["gpu"], "RTX-PRO-6000")
        stats.return_value = SimpleNamespace(num_total_runners=0, backlog=0)
        await self.decide(path, csrf, "approve")
        receipt = await (await self.send("warm-api", PAID, gpu="auto-speed")).json()
        self.assertEqual(receipt["gpu"], "CPU")
        self.assertNotIn("起動済み", receipt["gpu_reason"])
        self.assertEqual(await (await self.send("warm-api", PAID, gpu="auto-speed")).json(), receipt)
        self.assertEqual(len(self.control.journal.data["jobs"]), 1)
        self.assertEqual((await self.send("warm-api", PAID, gpu="auto")).status, 409)
        stats.assert_not_awaited()
        self.control.worker.spawn.aio.assert_not_awaited()

    async def test_stale_cookie_falls_back_but_invalid_explicit_gpu_is_rejected(self):
        browser = await (await self.client.post("/api/prompt", json={"prompt": PAID},
            headers={"Cookie": "split_gpu=retired-choice"})).json()
        self.assertEqual(browser["gpu"], "CPU")
        local = {"1": {"class_type": "MiniMaxMusic3TextEncode", "inputs": {}}}
        browser = await (await self.client.post("/api/prompt", json={"prompt": local},
            headers={"Cookie": "split_gpu=CPU"})).json()
        self.assertEqual(browser["gpu"], gpu.DEFAULT)
        response = await self.client.post("/api/prompt", json={"prompt": local, "gpu": "CPU"})
        self.assertEqual(response.status, 409)
        response = await self.client.post("/api/prompt", json={"prompt": PAID, "gpu": "retired-choice"})
        self.assertEqual(response.status, 409)

    async def test_disconnected_cloud_node_is_removed_before_local_execution(self):
        defs = {k: {**d, "output_node": False} for k, d in DEFS.items()}
        defs["SaveImage"] = {"python_module": "nodes", "output_node": True}
        self.control.objects.return_value = web.json_response(defs)
        graph = {
            **LOCAL,
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
            "3": PAID["1"],
        }
        self.assertEqual((await self.send("local-only", graph)).status, 200)
        job = next(iter(self.control.journal.data["jobs"].values()))
        self.assertEqual(set(job["body"]["prompt"]), {"1", "2"})
        self.assertFalse(job["mcp_guard"]["routes"])

    async def test_stdio_canvas_is_not_execution_metadata(self):
        r = await self.send(
            "stdio:job", LOCAL, extra_data={"extra_pnginfo": {"workflow": {"nodes": []}}}
        )
        self.assertEqual(r.status, 200)
        job = next(iter(self.control.journal.data["jobs"].values()))
        self.assertEqual(job["body"], {"client_id": "comfyui-mcp", "prompt": LOCAL})

    async def test_review_is_not_a_job_survives_restart_and_submits_once(self):
        path, csrf, html = await self.pending("render", PAID)
        self.assertIn("test-model", html)
        self.assertIn("雨の夜 &lt;script&gt;", html)
        self.assertIn(policy.COMFY, html)
        self.assertFalse(self.control.background_work())
        self.control.worker.spawn.aio.assert_not_awaited()
        self.assertEqual((await self.client.get(path)).status, 403)
        self.assertEqual((await self.decide(path, "forged", "approve")).status, 403)
        self.control.journal = Journal(self.control.journal.path.parent)
        self.assertEqual((await self.send("render", LOCAL)).status, 409)
        self.assertEqual(self.control.journal.data["jobs"], {})
        await asyncio.gather(self.decide(path, csrf, "approve"), self.decide(path, csrf, "approve"))
        result = await (await self.send("render", PAID)).json()
        self.assertEqual(list(self.control.journal.data["jobs"]), [result["prompt_id"]])
        row = self.control.journal.data["mcp_reviews"][path.split("/")[-1]]
        self.assertEqual(row["status"], "submitted")
        self.assertNotIn("csrf", row)

    async def test_denial_expiry_and_environment_change_never_enqueue(self):
        for state in ("denied", "expired", "invalidated"):
            path, csrf, _ = await self.pending(state, PAID)
            row = self.control.journal.data["mcp_reviews"][path.split("/")[-1]]
            if state == "expired":
                row["expires"] = time.time() - 10
            if state == "invalidated":
                self.control.journal.data["environment"] = "new"
            await self.decide(path, csrf, "deny" if state == "denied" else "approve")
            await self.decide(path, csrf, "approve")
            self.assertEqual(row["status"], state)
            self.assertNotIn("body", row)
            self.assertEqual((await self.send(state, PAID)).status, 409)
        self.assertEqual(self.control.journal.data["jobs"], {})

    async def test_only_the_reviewed_media_bytes_can_be_sent(self):
        f = self.input / "写真.png"
        Image.new("RGB", (2, 3), "red").save(f)
        original = f.read_bytes()
        graph = copy.deepcopy(PAID)
        graph["2"] = {"class_type": "LoadImage", "inputs": {"image": f.name}}
        graph["1"]["inputs"]["images"] = ["2", 0]
        path, csrf, html = await self.pending("media", graph)
        self.assertIn("写真.png", html)
        self.assertEqual((await self.client.get(path + "/media/0")).status, 403)
        f.write_bytes(b"replaced after review")
        self.assertEqual(
            await (await self.client.get(path + "/media/0", headers=self.review_headers)).read(),
            original,
        )
        response = await self.client.get(path + "/media/0", headers={**self.review_headers, "Range": "bytes=1-5"})
        self.assertEqual((response.status, await response.read()), (206, original[1:6]))
        self.assertEqual(response.headers["Content-Range"], f"bytes 1-5/{len(original)}")
        response = await self.client.get(path + "/media/0", headers={**self.review_headers, "Range": "bytes=999999-"})
        self.assertEqual(response.status, 416)
        await self.decide(path, csrf, "approve")
        job = next(iter(self.control.journal.data["jobs"].values()))
        snapshot = job["body"]["prompt"]["2"]["inputs"]["image"]
        self.assertEqual((self.input / snapshot).read_bytes(), original)
        (self.input / snapshot).write_bytes(b"different")
        with self.assertRaises(ValueError):
            policy.verify_assets(job["mcp_guard"]["assets"])

    async def test_deny_remains_available_during_approval_hashing_and_wins_the_race(self):
        path, csrf, _ = await self.pending("slow", PAID)
        started, release = threading.Event(), threading.Event()
        def slow_hash(assets):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test did not release verification")
        with patch.object(policy, "verify_assets", side_effect=slow_hash):
            task = asyncio.create_task(self.decide(path, csrf, "approve"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertFalse(self.control.lock.locked())
                denied = await asyncio.wait_for(self.decide(path, csrf, "deny"), 2)
                self.assertEqual(denied.status, 200)
            finally:
                release.set()
                await task
        row = self.control.journal.data["mcp_reviews"][path.split("/")[-1]]
        self.assertEqual(row["status"], "denied")
        self.assertFalse(self.control.journal.data["jobs"])

    async def test_preview_hashing_does_not_block_the_controller(self):
        f = self.input / "small.png"
        Image.new("RGB", (2, 3)).save(f)
        graph = copy.deepcopy(PAID)
        graph["2"] = {"class_type": "LoadImage", "inputs": {"image": f.name}}
        graph["1"]["inputs"]["images"] = ["2", 0]
        path, csrf, _ = await self.pending("slow-preview", graph)
        started, release = threading.Event(), threading.Event()
        original = approvals.hashlib.file_digest
        def slow_hash(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test did not release preview")
            return original(*args)
        with patch.object(approvals.hashlib, "file_digest", side_effect=slow_hash):
            task = asyncio.create_task(self.client.get(path + "/media/0", headers=self.review_headers))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertFalse(self.control.lock.locked())
                self.assertEqual((await asyncio.wait_for(self.decide(path, csrf, "deny"), 2)).status, 200)
            finally:
                release.set()
                response = await task
        self.assertEqual(response.status, 404)
        self.assertFalse(self.control.journal.data["jobs"])

    async def test_unknown_or_indirect_transmission_and_missing_key(self):
        for changes in (
            {"api_key": "do-not-store"},
            {"files": "123"},
            {"images": ["2", 0]},
            {"model": {"arbitrary": 1}},
        ):
            graph = copy.deepcopy(PAID)
            graph["1"]["inputs"].update(changes)
            graph["2"] = LOCAL["1"]
            self.assertEqual((await self.send("invalid", graph)).status, 409)
        self.assertEqual(
            (await self.send("invalid", PAID, extra_data={"api_key_comfy_org": "no"})).status, 409
        )
        with patch.dict("os.environ", SPLIT_COMFY_BILLING_ENABLED="0"):
            path, csrf, html = await self.pending("nokey", PAID)
            self.assertIn("未設定", html)
            await self.decide(path, csrf, "approve")
        self.assertFalse(self.control.background_work())

    async def test_gpu_refuses_graph_route_and_deployment_drift_before_any_provider_call(self):
        from unittest.mock import MagicMock

        from comfy_split import worker
        from comfy_split.config import DEPLOYMENT_ENV

        response = MagicMock()
        response.json = AsyncMock(return_value=DEFS)
        client = MagicMock()
        client.get.return_value.__aenter__.return_value = response
        path, csrf, _ = await self.pending("graph", PAID)
        await self.decide(path, csrf, "approve")
        job = next(iter(self.control.journal.data["jobs"].values()))
        spec = {"id": "test", "body": job["body"], "mcp_guard": job["mcp_guard"]}
        with patch.object(worker, "generate", new_callable=AsyncMock) as run:
            await worker.generate_with_extensions(spec, client, None, None)
            run.assert_awaited_once()
            for change in ("graph", "route", "deployment"):
                altered = copy.deepcopy(spec)
                if change == "graph":
                    altered["body"]["prompt"]["1"]["inputs"]["prompt"] = "different"
                if change == "route":
                    altered["mcp_guard"]["routes"][0][3] = "different"
                with (
                    patch.dict(
                        "os.environ", {DEPLOYMENT_ENV: "changed"} if change == "deployment" else {}
                    ),
                    self.assertRaisesRegex(ValueError, "changed"),
                ):
                    await worker.generate_with_extensions(altered, client, None, None)
            self.assertEqual(run.await_count, 1)
            client.post.assert_not_called()

    async def test_only_approved_partner_prompt_receives_server_key(self):
        from unittest.mock import MagicMock

        from comfy_split import worker

        client = MagicMock()
        # Reject at the fake loopback. Nothing reaches an external service.
        rejected = client.post.return_value.__aenter__.return_value
        rejected.status = 400
        rejected.json = AsyncMock(return_value={})
        spec = {"id": "job", "body": {"prompt": PAID}, "mcp_guard": {"routes": []}}
        with patch.dict("os.environ", COMFY_API_KEY="test-key"):
            for billing in ("local", policy.COMFY):
                if billing == policy.COMFY:
                    spec["mcp_guard"]["routes"] = [["1", "GeminiNode", "Gemini", billing, None]]
                await worker.generate(spec, client, None, None)
                on_wire = client.post.call_args.kwargs["json"]
                if billing == policy.COMFY:
                    self.assertEqual(on_wire["extra_data"], {"api_key_comfy_org": "test-key"})
                else:
                    self.assertNotIn("extra_data", on_wire)
                self.assertNotIn("extra_data", spec["body"])

    def test_minimax_local_vs_cloud_and_direct_vs_partner_billing(self):
        for name, expected in (
            ("MiniMaxMusic3TextEncode", None),
            ("ComfyCloudMiniMaxMusic3TextToAudioNode", policy.COMFY),
            ("OpenAIChatNode", policy.COMFY),
            ("OpenRouterLLMNode", policy.COMFY),
            ("OpenRouterText", "OpenRouter アカウント"),
        ):
            calls, _ = policy.plan({"1": {"class_type": name, "inputs": {}}}, DEFS)
            if expected:
                self.assertIn(expected, calls[0]["billing"])
            else:
                self.assertFalse(calls)

    def test_direct_provider_labels_survive_the_runtime_environment(self):
        labels = {"SPLIT_GEMINI_BILLING_LABEL": "Example Google project",
                  "SPLIT_OPENROUTER_BILLING_LABEL": "Example OpenRouter account"}
        with patch.dict("os.environ", labels, clear=True):
            self.assertEqual(policy.billing_account("Google Gemini アカウント"), labels["SPLIT_GEMINI_BILLING_LABEL"])
            calls, _ = policy.plan({"1": {"class_type": "OpenRouterText", "inputs": {}}}, DEFS)
            self.assertEqual(calls[0]["account"], labels["SPLIT_OPENROUTER_BILLING_LABEL"])
