from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from uuid import uuid4

from .h3 import workflow
from .urls import redirect_guard, validate_endpoint


def output_file(history: dict) -> dict:
    for output in history.get("outputs", {}).values():
        for key in ("images", "gifs", "videos"):
            for item in output.get(key, []):
                if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(
                    (".mp4", ".webm")
                ):
                    return {k: item[k] for k in ("filename", "subfolder", "type") if k in item}
    raise RuntimeError("ComfyUI finished without a video output")


async def generate(
    base: str,
    headers: dict,
    request: dict,
    image: Path | None,
    destination: Path,
    cancelled,
    progress,
    timeout: int = 1800,
) -> None:
    import aiohttp

    base = validate_endpoint(base, allow_http_loopback=not headers)
    client_id = str(uuid4())
    async with aiohttp.ClientSession(
        headers=headers, timeout=aiohttp.ClientTimeout(total=120), trace_configs=[redirect_guard()]
    ) as session:

        async def call(method, path, **kwargs):
            async with session.request(method, base.rstrip("/") + path, **kwargs) as response:
                if response.status >= 400:
                    raise RuntimeError(
                        f"ComfyUI {path}: HTTP {response.status}: {(await response.text())[:600]}"
                    )
                return await response.json()

        # This control-only backend connection keeps the existing web_server input active.
        # Never forward preview bytes, and disable compression for Modal's proxy.
        async with session.ws_connect(
            base.rstrip("/") + "/ws?clientId=" + client_id, compress=0, heartbeat=15
        ) as socket:

            async def drain():
                async for message in socket:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        event = json.loads(message.data)
                        if event.get("type") == "progress":
                            progress("Sampling")

            drain_task = asyncio.create_task(drain())
            try:
                objects = await call("GET", "/object_info")
                # Resolve contracts before uploading or submitting any generation.
                workflow(request, "ambient/anchor.png" if image else None, object_info=objects)
                image_name = None
                if image:
                    form = aiohttp.FormData()
                    form.add_field(
                        "image",
                        image.read_bytes(),
                        filename=f"ambient-{request['requestId']}.png",
                        content_type="image/png",
                    )
                    form.add_field("type", "input")
                    form.add_field("subfolder", "ambient/uploads")
                    uploaded = await call("POST", "/upload/image", data=form)
                    image_name = "/".join(
                        filter(None, [uploaded.get("subfolder"), uploaded["name"]])
                    )
                submitted = await call(
                    "POST",
                    "/prompt",
                    json={
                        "prompt": workflow(request, image_name, object_info=objects),
                        "client_id": client_id,
                    },
                )
                if submitted.get("node_errors"):
                    raise RuntimeError(f"H3 workflow validation failed: {submitted['node_errors']}")
                prompt_id = submitted["prompt_id"]
                progress("ComfyUI queued")
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    history = (await call("GET", f"/history/{prompt_id}")).get(prompt_id)
                    if history:
                        if cancelled():
                            return
                        if history.get("status", {}).get("status_str") == "error":
                            raise RuntimeError(
                                f"ComfyUI generation failed: {history.get('status', {}).get('messages', [])[-1:]}"
                            )
                        if not history.get("status", {}).get("completed", False):
                            await asyncio.sleep(1)
                            continue
                        artifact = output_file(history)
                        progress("Downloading generated video")
                        # Allow a slow but progressing transfer, bounded by the job deadline.
                        download_timeout = aiohttp.ClientTimeout(
                            total=None, sock_connect=30, sock_read=120
                        )
                        async with (
                            asyncio.timeout(max(0, deadline - time.monotonic())),
                            session.get(
                                base + "/view", params=artifact, timeout=download_timeout
                            ) as response,
                        ):
                            response.raise_for_status()
                            with destination.open("wb") as handle:
                                async for chunk in response.content.iter_chunked(1024 * 1024):
                                    handle.write(chunk)
                        return
                    if cancelled():
                        queue = await call("GET", "/queue")
                        # Deleting this ID from pending is safe even if it just began running.
                        await call("POST", "/queue", json={"delete": [prompt_id]})
                        running = any(row[1] == prompt_id for row in queue.get("queue_running", []))
                        if not running:
                            return
                        # Logical cancellation: drain only our running job. Never /interrupt.
                    if socket.closed:
                        raise RuntimeError(
                            "ComfyUI control connection closed; job result is uncertain. Inspect ComfyUI history before retrying."
                        )
                    await asyncio.sleep(1)
                raise TimeoutError(
                    "ComfyUI generation timed out; upstream result may still be running"
                )
            finally:
                drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)
