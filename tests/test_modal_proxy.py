"""Exercise the real pinned Modal proxy against ComfyUI-style file routes."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from comfy_split.modal_proxy import web_server_proxy


class ModalProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.files = {}

        async def userdata(request):
            name = request.match_info["file"]
            if request.method == "POST":
                self.files[name] = await request.read()
                return web.json_response(name)
            if request.method == "DELETE":
                self.files.pop(name)
                return web.Response(status=204)
            if name not in self.files:
                raise web.HTTPNotFound()
            return web.Response(body=self.files[name], content_type="application/json")

        async def move(request):
            source, dest = request.match_info["file"], request.match_info["dest"]
            self.files[dest] = self.files.pop(source)
            return web.json_response({"path": dest, "full_info": request.query.get("full_info")})

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"path": request.raw_path})
            async for message in ws:
                await ws.send_str(message.data)
            return ws

        app = web.Application()
        app.router.add_route("*", "/api/userdata/{file}", userdata)
        app.router.add_post("/api/userdata/{file}/move/{dest}", move)
        app.router.add_get("/ws/{file}", socket)
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = ClientSession(str(self.server.make_url("/")), auto_decompress=False)
        proxy = web_server_proxy(self.server.host, self.server.port)

        async def with_state(scope, receive, send):
            await proxy({**scope, "state": {"session": self.session}}, receive, send)

        self.app = with_state

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def request(self, method, url, body=b"", *, modal_wire=False):
        parts = urlsplit(url)
        incoming, outgoing = asyncio.Queue(), []
        await incoming.put({"type": "http.request", "body": body, "more_body": False})

        async def send(message):
            outgoing.append(message)

        scope = {"type": "http", "method": method, "path": unquote(parts.path),
                 "raw_path": parts.path.encode("ascii"), "query_string": parts.query.encode(),
                 "headers": [(b"content-type", b"application/json")],
                 "http_version": "1.1", "scheme": "https"}
        if modal_wire:
            from modal._serialization import _deserialize_asgi, _serialize_asgi
            scope = _deserialize_asgi(_serialize_asgi(scope))
        await asyncio.wait_for(self.app(scope, incoming.get, send), 5)
        content = b"".join(message.get("body", b"") for message in outgoing)
        return SimpleNamespace(status_code=outgoing[0]["status"], json=lambda: json.loads(content))

    async def test_workflow_save_open_rename_and_delete(self):
        await self.check_workflow_operations(modal_wire=False)

    async def test_workflows_through_modals_actual_scope_serialization(self):
        await self.check_workflow_operations(modal_wire=True)

    async def check_workflow_operations(self, *, modal_wire):
        for name in ("workflows/video_minimax_h3_i2v.json", "workflows/日本語 #?% +/動画.json",
                     "workflows/move/scene.json"):
            with self.subTest(name=name):
                path = "/api/userdata/" + quote(name, safe="")
                saved = await self.request("POST", path, json.dumps({"nodes": [{"id": 1}]}).encode(), modal_wire=modal_wire)
                self.assertEqual(saved.status_code, 200)
                self.assertIn(name, self.files)
                loaded = await self.request("GET", path, modal_wire=modal_wire)
                self.assertEqual(loaded.status_code, 200)
                self.assertEqual(loaded.json(), {"nodes": [{"id": 1}]})
                dest = "workflows/renamed.json"
                moved = await self.request("POST", path + "/move/" + quote(dest, safe="") + "?full_info=true", modal_wire=modal_wire)
                self.assertEqual(moved.status_code, 200)
                self.assertEqual(moved.json(), {"path": dest, "full_info": "true"})
                deleted = await self.request("DELETE", "/api/userdata/" + quote(dest, safe=""), modal_wire=modal_wire)
                self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.files, {})

    async def test_websocket_path_and_frames_still_forward(self):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        scope = {"type": "websocket", "path": "/ws/folder/file", "raw_path": b"/ws/folder%2Ffile",
                 "query_string": b"clientId=test", "headers": [], "subprotocols": []}
        task = asyncio.create_task(self.app(scope, incoming.get, outgoing.put))
        try:
            self.assertEqual((await asyncio.wait_for(outgoing.get(), 5))["type"], "websocket.accept")
            message = await asyncio.wait_for(outgoing.get(), 5)
            self.assertIn("/ws/folder%2Ffile?clientId=test", message["text"])
            await incoming.put({"type": "websocket.receive", "text": "hello"})
            self.assertEqual((await asyncio.wait_for(outgoing.get(), 5))["text"], "hello")
        finally:
            await incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 5)
