"""Raw HTTP clients cannot bypass the MCP approval path or forge gateway access."""

import json
import os
import sys
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from comfy_split.gateway_access import ENV, HEADER, boundary
from comfy_split.modal_proxy import web_server_proxy
from comfy_split import remote_mcp
from comfy_split.proxy import proxy


class GatewayAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def success(request):
            return web.Response(text="handled")
        app = web.Application(middlewares=[boundary("private-fixture")])
        app.router.add_route("*", "/{rest:.*}", success)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_unwrapped_child_http_client_cannot_submit_or_administer(self):
        for path in ("/prompt", "/api/prompt", "/_remote_mcp/submit", "/api/_remote_mcp/submit",
                     "/_remote_mcp/review/example", "/interrupt", "/queue", "/free",
                     "/split/environment/apply", "/_remote_mcp/cancel"):
            response = await self.client.post(path, headers={HEADER: "forged",
                "X-Split-Browser-Review": "1", "X-Split-Remote-Tool": "1"}, json={"prompt": {}})
            self.assertEqual(response.status, 403, path)
        self.assertEqual((await self.client.get("/ws")).status, 403)
        self.assertEqual((await self.client.post("/prompt", headers={HEADER: "private-fixture"})).status, 200)

    async def test_only_reads_and_new_library_files_are_allowed_without_capability(self):
        for path in ("/api/object_info/SaveImage", "/models/checkpoints", "/queue", "/history/example",
                     "/api/userdata?dir=workflows", "/split/startup"):
            self.assertEqual((await self.client.get(path)).status, 200, path)
        save = "/api/userdata/workflows%2Ffolder%2Ftest.json"
        self.assertEqual((await self.client.post(URL(save + "?overwrite=false", encoded=True), json={})).status, 200)
        for path in (save, save + "?overwrite=true", save + "?overwrite=false&overwrite=true",
                     "/api/userdata/comfy.settings.json?overwrite=false",
                     "/api/userdata/workflows%2F..%2Fsettings.json?overwrite=false",
                     save + "/move/workflows%2Fother.json?overwrite=false"):
            self.assertEqual((await self.client.post(URL(path, encoded=True), json={})).status, 403, path)
        self.assertEqual((await self.client.delete(URL(save, encoded=True))).status, 403)

    async def test_proxy_replaces_forged_header_for_http_and_websocket(self):
        scopes = []
        async def upstream(scope, receive, send):
            scopes.append(scope)
        with patch("modal._runtime.asgi.web_server_proxy", return_value=upstream):
            app = web_server_proxy("localhost", 8000, gateway_token="private-fixture")
        for kind in ("http", "websocket"):
            await app({"type": kind, "path": "/prompt", "headers": [
                (HEADER.encode(), b"forged"), (HEADER.lower().encode(), b"another-forgery")]}, None, None)
        for scope in scopes:
            self.assertEqual(scope["headers"], [(HEADER.lower().encode(), b"private-fixture")])

    async def test_remote_subprocess_does_not_inherit_capability_or_provider_keys(self):
        script = "import os,json; print(json.dumps({k:v for k,v in os.environ.items() if k in ['SPLIT_GATEWAY_TOKEN','GEMINI_API_KEY','OPENROUTER_API_KEY']}))"
        with patch.dict(os.environ, {ENV: "private-fixture", "GEMINI_API_KEY": "secret", "OPENROUTER_API_KEY": "secret"}), \
             patch.object(remote_mcp, "BRIDGE_COMMAND", (sys.executable, "-c", script)):
            result = await remote_mcp.run_tool({})
        self.assertEqual(json.loads(result.body), {})

    async def test_gateway_capability_never_reaches_comfyui_http_or_websocket(self):
        async def echo(request):
            if request.path == "/ws":
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.send_json(dict(request.headers))
                await ws.close()
                return ws
            return web.json_response(dict(request.headers))
        target = web.Application()
        target.router.add_route("*", "/{rest:.*}", echo)
        server = TestServer(target)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        session = ClientSession()
        self.addAsyncCleanup(session.close)
        async def forward(request):
            return await proxy(request, session, str(server.make_url("/")))
        front = web.Application()
        front.router.add_route("*", "/{rest:.*}", forward)
        client = TestClient(TestServer(front))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.get("/object_info", headers={HEADER: "private-fixture"})
        self.assertNotIn(HEADER.lower(), {k.lower() for k in await response.json()})
        async with client.ws_connect("/ws", headers={HEADER: "private-fixture"}) as ws:
            self.assertNotIn(HEADER.lower(), {k.lower() for k in await ws.receive_json()})

