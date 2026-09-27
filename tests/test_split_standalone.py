"""The default split image and gateway must work without any Ambient package."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from comfy_split.config import NODE_PACKS, Settings
from comfy_split.gateway import Controller

ROOT = Path(__file__).resolve().parents[1]
PLAIN = {"SPLIT_EXTENSIONS": "", "SPLIT_NODE_PACKS": "", "SPLIT_AGENT_BRIDGE": "off"}


class StandaloneTests(unittest.TestCase):
    def test_plain_deployment_never_imports_or_bundles_ambient_or_requests_secrets(self):
        script = '''
import importlib.abc, json, runpy, sys
from unittest.mock import patch
class ForbidAmbient(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'ambient', 'ambient_app', 'ambient_comfyui', 'bundled_packages'}:
            raise AssertionError('Unexpected dependency: ' + fullname)
sys.meta_path.insert(0, ForbidAmbient())
import comfyapp, modal
from comfy_split import gateway, runtime, cpu_snapshot, worker
sources = []
original = modal.Image.add_local_dir
def record(image, source, *args, **kwargs):
    sources.append(str(source))
    return original(image, source, *args, **kwargs)
with patch.object(modal.Secret, 'from_name', side_effect=AssertionError('Unexpected Secret')), patch.object(modal.Image, 'add_local_dir', record):
    app = runpy.run_path('splitapp.py')
assert app['settings'].extensions == ()
assert app['provider_secrets'] == app['github_secrets'] == []
assert not any('ambient' in source.lower() for source in sources)
import unittest
sys.path.insert(0, 'tests')
# Exercise runtime paths too: import isolation must survive start/restore,
# queue acceptance, dispatch, history, cancellation and CPU/GPU shutdown.
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'test_comfy_split', 'test_cpu_snapshot', 'test_split_startup', 'test_split_storage',
])
if not unittest.TextTestRunner(verbosity=0).run(suite).wasSuccessful():
    raise SystemExit(1)
print(json.dumps({'standalone': True}))
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
            env={**os.environ, **PLAIN}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"standalone": true', result.stdout)

    def test_new_settings_override_legacy_individually_including_empty(self):
        self.assertEqual(Settings.read({}), Settings())
        old = {"COMFYUI_AMBIENT_MODE": "on"}
        self.assertEqual(Settings.read(old).node_packs, tuple(NODE_PACKS))
        self.assertEqual(Settings.read({**old, **PLAIN}), Settings())
        self.assertEqual(Settings.read({**old, "SPLIT_EXTENSIONS": ""}).extensions, ())
        for key, value in (("SPLIT_EXTENSIONS", "typo"), ("SPLIT_NODE_PACKS", "typo"),
                           ("SPLIT_AGENT_BRIDGE", "yes")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Settings.read({key: value})

    def test_node_packs_and_bridge_do_not_enable_ambient_or_unrelated_secrets(self):
        env = {"SPLIT_NODE_PACKS": "gemini", "SPLIT_AGENT_BRIDGE": "on",
               "GEMINI_SECRET_NAME": "gemini", "TYPESAFE_SECRET_NAME": "unused",
               "OPENROUTER_SECRET_NAME": "unused", "AGENT_RUNTIME_SECRET_NAME": "bridge"}
        config = Settings.read(env)
        self.assertEqual(config.extensions, ())
        self.assertEqual(config.node_packs, ("gemini",))
        self.assertEqual([name for name, _, _ in config.secrets(env)],
                         ["GEMINI_SECRET_NAME", "AGENT_RUNTIME_SECRET_NAME"])


class PlainGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, PLAIN)
        env.start()
        self.addCleanup(env.stop)
        remote = lambda: SimpleNamespace(aio=AsyncMock())
        self.worker = SimpleNamespace(spawn=remote())
        volumes = {key: SimpleNamespace(commit=remote()) for key in ("input", "data")}
        self.controller = Controller(self.worker, None, None, volumes, Path(self.temp.name))
        from aiohttp import ClientSession
        self.controller.client = ClientSession()
        async def missing(_request):
            raise web.HTTPNotFound()
        cpu = web.Application()
        cpu.router.add_route("*", "/{path:.*}", missing)
        self.cpu = TestServer(cpu)
        await self.cpu.start_server()
        self.controller.cpu = SimpleNamespace(url=str(self.cpu.make_url("/")).rstrip("/"))
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.controller.handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.controller.client.close()
        await self.cpu.close()

    async def test_plain_prompt_is_idempotent_and_optional_endpoints_are_absent(self):
        self.assertEqual(self.controller.plugins, [])
        body = {"prompt": {"1": {"class_type": "SaveImage", "inputs": {}}}}
        responses = [await self.client.post("/prompt", json=body, headers={"Idempotency-Key": "plain"}) for _ in range(2)]
        self.assertEqual([r.status for r in responses], [200, 200])
        self.assertEqual((await responses[0].json())["prompt_id"], (await responses[1].json())["prompt_id"])
        for path in ("/ambient/workflows", "/ambient/executions", "/ambient/library", "/agent_runtime/bridge/catalog"):
            self.assertEqual((await self.client.get(path)).status, 404)
        self.worker.spawn.aio.assert_not_awaited()

    async def test_disabled_bridge_rejects_before_acceptance(self):
        response = await self.client.post("/prompt", json={"prompt": {
            "1": {"class_type": "AgentRuntimeBridgeText", "inputs": {}}}})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.controller.journal.data["jobs"], {})
        self.worker.spawn.aio.assert_not_awaited()

    async def test_optional_observer_failure_does_not_block_native_events(self):
        def broken(_event):
            raise RuntimeError("Extension failure")
        self.controller.plugins = [SimpleNamespace(event=broken)]
        socket = SimpleNamespace(send_json=AsyncMock())
        self.controller.sockets = {"native": [socket]}
        event = {"type": "progress", "data": {"value": 1, "max": 2}}
        with self.assertLogs("comfy_split.gateway", level="ERROR"):
            await self.controller.broadcast(event)
        socket.send_json.assert_awaited_once_with(event)
