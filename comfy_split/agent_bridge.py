"""Route the native AgentRuntime Bridge protocol across the CPU/GPU boundary.

The Mac stays connected to the CPU. Only a workflow containing a Bridge node
opens a temporary GPU tunnel. Queue messages carry readiness, never user files
or model output. Both ComfyUI processes use the unmodified upstream BridgeHub.
"""

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager

import modal
from aiohttp import ClientError, WSMsgType, web

from comfy_split.proxy import proxy
from comfy_split.bridge_transport import HttpChannel, HttpSocket, TRANSPORT

PREFIX = "/agent_runtime/bridge"
TOKEN_ENV = "AGENT_RUNTIME_BRIDGE_TOKEN"
MAX_MESSAGE = 20 * 1024 * 1024
FILE_ROUTE = re.compile(PREFIX + r"/jobs/([a-zA-Z0-9_-]+)/(inputs/[a-zA-Z0-9_-]+|artifacts)$")


def requires_bridge(body):
    prompt = body.get("prompt", {}) if isinstance(body, dict) else {}
    return isinstance(prompt, dict) and any(
        isinstance(node, dict) and str(node.get("class_type", "")).startswith("AgentRuntimeBridge")
        for node in prompt.values())


def authorize(request, token):
    if not token:
        raise web.HTTPServiceUnavailable(text="Set AGENT_RUNTIME_BRIDGE_TOKEN in the Modal Secret.")
    if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
        raise web.HTTPUnauthorized(text="Invalid bridge token.")


def normalized_path(request):
    path = request.rel_url.raw_path_qs
    return path[4:] if path.startswith("/api/") else path


async def connect(client, url, token, hello, *, http_transport=False):
    async with asyncio.timeout(15):
        socket = await client.ws_connect(url + PREFIX + "/ws",
            headers={"Authorization": "Bearer " + token}, heartbeat=15,
            compress=0, max_msg_size=MAX_MESSAGE)
        try:
            wire_hello = hello
            if http_transport:
                wire_hello = dict(hello, transport=TRANSPORT, catalog={
                    "models": [], "skills": [], "revision": "", "auth": "unknown"})
            await socket.send_json(wire_hello)
            ready = await socket.receive_json()
            if ready.get("type") != "ready" or ready.get("version") != 1:
                raise ValueError("Incompatible AgentRuntime Bridge protocol")
            if http_transport:
                if ready.get("transport") != TRANSPORT or not ready.get("session"):
                    raise ValueError("GPU does not support HTTP Bridge payloads")
                return HttpSocket(socket, client, url, token, ready["session"]), ready
            return socket, ready
        except BaseException:
            await socket.close()
            raise


class AgentBridge:
    def __init__(self, controller):
        self.controller = controller
        self.socket = None
        self.connection_id = None
        self.hello = None
        self.cpu = None
        self.gpu = None
        self.gpu_task = None
        self.record_id = None
        self.url = None
        self.token = None
        self.jobs = set()
        self.channel = None

    def identity(self):
        if not self.connection_id or self.socket is None or self.socket.closed:
            raise ValueError("MacのAgentRuntime Bridgeを接続してから実行してください。")
        return self.connection_id

    async def handle(self, request):
        path = request.path.removeprefix("/api")
        if path == PREFIX + "/catalog" and request.method == "GET":
            return await proxy(request, self.controller.client, self.controller.cpu.url,
                               path=normalized_path(request))
        authorize(request, os.environ.get(TOKEN_ENV, ""))
        if path == PREFIX + "/messages" or path.startswith(PREFIX + "/messages/"):
            if self.channel is None:
                raise web.HTTPConflict(text="Connect Bridge first")
            return await self.channel.http(request)
        if path == PREFIX + "/ws" and request.method == "GET":
            return await self.websocket(request)
        match = FILE_ROUTE.fullmatch(path)
        if not match or match[1] not in self.jobs or not self.url:
            raise web.HTTPNotFound(text="Bridge job is no longer active.")
        return await proxy(request, self.controller.client, self.url, self.token,
                           path=normalized_path(request))

    async def websocket(self, request):
        if self.socket is not None:
            raise web.HTTPConflict(text="A Mac backend is already connected.")
        socket = web.WebSocketResponse(heartbeat=15, max_msg_size=MAX_MESSAGE)
        self.socket = socket  # Reserve before the first await, including handshake.
        monitor = None
        try:
            await socket.prepare(request)
            hello = await socket.receive_json(timeout=15)
            # Native CPU BridgeHub validates identity, version and catalog.
            self.cpu, ready = await connect(self.controller.client, self.controller.cpu.url,
                                            os.environ[TOKEN_ENV], hello)
            self.hello = hello
            self.connection_id = secrets.token_hex(16)
            self.channel = HttpChannel(socket, self.connection_id, hello.get("transport") == TRANSPORT)
            if self.channel.enabled:
                ready = dict(ready, transport=TRANSPORT, session=self.connection_id)
            await socket.send_json(ready)

            async def watch_cpu():
                async for _ in self.cpu:
                    pass
                # A restarted CPU has lost its catalog. Reconnect both ends.
                await socket.close(code=1012, message=b"ComfyUI restarted; reconnect Bridge")

            monitor = asyncio.create_task(watch_cpu())
            async for message in socket:
                if message.type != WSMsgType.TEXT:
                    continue
                raw = self.channel.decode(message.data)
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Expected a Bridge message object")
                if data.get("type") == "catalog":
                    self.hello = dict(self.hello, catalog=data["catalog"])
                    await self.cpu.send_str(raw)
                    if self.gpu is not None:
                        await self.gpu.send_str(raw)
                elif data.get("type") in {"event", "result"} and data.get("id") in self.jobs:
                    await self.gpu.send_str(raw)
                    if data["type"] == "result":
                        self.jobs.discard(data["id"])
        except (ValueError, KeyError, TypeError, TimeoutError):
            await socket.close(code=1008, message=b"Invalid Bridge handshake or message")
        except (ClientError, ConnectionError, RuntimeError):
            await socket.close(code=1011, message=b"Bridge connection lost")
        finally:
            self.connection_id = None
            if self.channel:
                self.channel.clear()
            await self.detach()
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            if self.cpu is not None:
                await self.cpu.close()
            self.cpu = self.hello = self.socket = None
            self.channel = None
        return socket

    async def attach(self, record, event):
        if record.get("agent_bridge") != self.identity():
            raise ValueError("Mac Bridgeの接続が変わりました。ワークフローを再実行してください。")
        if self.record_id == record["id"]:
            if self.gpu is None or self.gpu.closed:
                raise ValueError("GPU Bridge disconnected; execution will not be replayed")
            return
        await self.detach()
        # Keep ownership even after a failed connection to prevent silent retries.
        self.record_id = record["id"]
        socket, _ = await connect(self.controller.client, event["url"], event["token"], self.hello,
                                  http_transport=event.get("transport") == TRANSPORT)
        try:
            if record["agent_bridge"] != self.identity():
                raise ValueError("Mac Bridge disconnected while attaching GPU")
            await socket.send_json({"type": "catalog", "catalog": self.hello["catalog"]})
        except BaseException:
            await socket.close()
            raise
        self.gpu = socket
        self.url, self.token = event["url"], event["token"]

        async def relay():
            try:
                async for message in socket:
                    if message.type != WSMsgType.TEXT:
                        continue
                    data = json.loads(message.data)
                    if data.get("type") == "job":
                        self.jobs.add(data["id"])
                    elif data.get("type") == "cancel":
                        self.jobs.discard(data["id"])
                    else:
                        continue
                    await self.channel.send_str(message.data)
            finally:
                await self.cancel_jobs()
                self.url = self.token = None
                await socket.close()

        self.gpu_task = asyncio.create_task(relay())

    async def cancel_jobs(self):
        ids, self.jobs = self.jobs, set()
        for job_id in ids:
            if self.socket is not None and not self.socket.closed:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await self.socket.send_json({"type": "cancel", "id": job_id})

    async def detach(self, record_id=None):
        if record_id is not None and self.record_id != record_id:
            return
        if self.gpu_task:
            self.gpu_task.cancel()
            await asyncio.gather(self.gpu_task, return_exceptions=True)
        if self.gpu is not None:
            await self.gpu.close()
        await self.cancel_jobs()
        self.gpu = self.gpu_task = self.record_id = self.url = self.token = None

    async def close(self):
        if self.socket is not None:
            await self.socket.close(code=1001, message=b"Gateway shutting down")
        await self.detach()
        if self.cpu is not None:
            await self.cpu.close()


@asynccontextmanager
async def gpu_tunnel(client, origin):
    """Only native Bridge routes are reachable through this per-job tunnel."""
    token = secrets.token_urlsafe(32)
    sockets = set()
    channel = None

    async def handler(request):
        nonlocal channel
        authorize(request, token)
        path = request.path
        if path == PREFIX + "/messages" or path.startswith(PREFIX + "/messages/"):
            if channel is None:
                raise web.HTTPConflict(text="Connect Bridge first")
            return await channel.http(request)
        if path == PREFIX + "/ws" and request.method == "GET":
            if channel is not None:
                raise web.HTTPConflict()
            socket = web.WebSocketResponse(heartbeat=15, max_msg_size=MAX_MESSAGE)
            channel = HttpChannel(socket, secrets.token_hex(16), False)
            sockets.add(socket)
            upstream = None
            tasks = []
            try:
                await socket.prepare(request)
                hello = await socket.receive_json(timeout=15)
                upstream, ready = await connect(client, origin, os.environ.get(TOKEN_ENV, ""), hello)
                channel.enabled = hello.get("transport") == TRANSPORT
                if channel.enabled:
                    ready = dict(ready, transport=TRANSPORT, session=channel.session)
                await socket.send_json(ready)

                async def incoming():
                    async for message in socket:
                        if message.type == WSMsgType.TEXT:
                            await upstream.send_str(channel.decode(message.data))

                async def outgoing():
                    async for message in upstream:
                        if message.type == WSMsgType.TEXT:
                            await channel.send_str(message.data)

                tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                channel.clear()
                if upstream is not None:
                    await upstream.close()
                await socket.close()
                sockets.discard(socket)
                channel = None
            return socket
        if not (path == PREFIX + "/ws" and request.method == "GET" or
                FILE_ROUTE.fullmatch(path) and request.method in {"GET", "POST"}):
            raise web.HTTPNotFound()
        return await proxy(request, client, origin, os.environ.get(TOKEN_ENV), sockets=sockets)

    async def shutdown(app):
        await asyncio.gather(*(socket.close(code=1001) for socket in list(sockets)))

    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.router.add_route("*", "/{path:.*}", handler)
    app.on_shutdown.append(shutdown)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", 8191).start()
        async with modal.forward(8191) as tunnel:
            yield {"type": "agent_bridge_ready", "url": tunnel.url, "token": token, "transport": TRANSPORT}
    finally:
        await runner.cleanup()


async def await_connection(ready, control, emit):
    deadline, next_announce = time.monotonic() + 60, 0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_announce:
            await emit(ready)
            next_announce = time.monotonic() + 5
        commands = await control()
        if any(item["type"] == "interrupt" for item in commands):
            return False
        if any(item["type"] == "agent_bridge_error" for item in commands):
            raise RuntimeError("Mac BridgeをGPUへ接続できませんでした。接続を確認して再実行してください。")
        if any(item["type"] == "agent_bridge_connected" for item in commands):
            return True
        await asyncio.sleep(0.2)
    raise TimeoutError("Timed out connecting Mac Bridge to GPU")
