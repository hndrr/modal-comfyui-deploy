"""Run an isolated, short-lived stdio MCP client after the split startup gate."""
import asyncio
import json
import os
import signal
import tempfile
from aiohttp import web

_slots = asyncio.Semaphore(4)


async def call(request):
    # Only the remote Worker adds this marker, after authentication. The UI
    # Worker strips it and the Modal origin itself requires Proxy Auth.
    if (request.method != "POST" or request.headers.get("X-Split-Remote-Tool") != "1"
            or request.headers.get("X-Modal-Execution-Mode") != "split"):
        raise web.HTTPForbidden()
    payload = await request.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("arguments"), dict):
        raise web.HTTPBadRequest()
    async with _slots, asyncio.timeout(55):
        with tempfile.TemporaryDirectory(prefix="split-mcp-") as directory:
            proc = await asyncio.create_subprocess_exec(
                "/usr/local/bin/node", "/opt/split-mcp/remote-call.mjs", cwd=directory,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": directory},
                start_new_session=True, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                output, _ = await proc.communicate(json.dumps(payload).encode())
                if proc.returncode or len(output) > 10 * 1024 * 1024:
                    raise web.HTTPBadGateway(text="MCP tool failed")
                return web.json_response(json.loads(output))
            finally:
                # Kill descendants too if the caller disconnected or timed out.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
