"""Run an isolated, short-lived stdio MCP client after the split startup gate."""
import asyncio
import json
import os
import signal
import tempfile
from aiohttp import web

BRIDGE_COMMAND = ("/usr/local/bin/node", "/opt/split-mcp/remote-call.mjs")
CALL_TIMEOUT = 55
_slots = asyncio.Semaphore(4)


def authorize(request, mode):
    # Only the remote Worker adds this marker, after authentication. The UI
    # Worker strips it and the Modal origin itself requires Proxy Auth.
    if (request.method != "POST" or request.headers.get("X-Split-Remote-Tool") != "1"
            or request.headers.get("X-Modal-Execution-Mode") != "split"):
        raise web.HTTPForbidden()
    if mode != "split":
        raise web.HTTPConflict(text='{"error":"This client requires split mode."}', content_type="application/json")


async def call(request, *, mode):
    authorize(request, mode)
    payload = await request.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("arguments"), dict):
        raise web.HTTPBadRequest()
    try:
        async with _slots, asyncio.timeout(CALL_TIMEOUT):
            return await run_tool(payload)
    except TimeoutError:
        raise web.HTTPGatewayTimeout(text="MCP tool timed out") from None


async def run_tool(payload):
    with tempfile.TemporaryDirectory(prefix="split-mcp-") as directory:
        proc = await asyncio.create_subprocess_exec(
            *BRIDGE_COMMAND, cwd=directory,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": directory},
            start_new_session=True, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await proc.communicate(json.dumps(payload).encode())
            if proc.returncode or len(output) > 10 * 1024 * 1024:
                raise web.HTTPBadGateway(text="MCP tool failed")
            try:
                result = json.loads(output)
            except ValueError:
                raise web.HTTPBadGateway(text="Invalid MCP response") from None
            return web.json_response(result)
        finally:
            # Also kill descendants if the caller disconnects or times out.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
