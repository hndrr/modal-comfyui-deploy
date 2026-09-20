"""Serve a small startup page while the CPU environment becomes ready."""

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from aiohttp import web

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("startup.html").read_text()
NO_CACHE = {"Cache-Control": "no-store"}


class StartupGate:
    def __init__(self, controller, *, request_wait=20):
        self.controller = controller
        self.request_wait = request_wait
        self.task = None
        self.finished = asyncio.Event()
        self.ready = False
        self.failed = False
        self.started = time.monotonic()

    async def start(self, app):
        # Binding HTTP must not wait for Git, Volume copies, pip or Comfy imports.
        self.controller.starting = True
        self.task = asyncio.create_task(self.initialize(app))

    async def initialize(self, app):
        try:
            await self.controller.pin_cpu(True)
            await self.controller.start(app)
            self.ready = True
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            log.exception("CPU startup failed")
        finally:
            self.controller.starting = False
            try:
                await self.controller.reconcile_cpu_scaling()
            except Exception:
                log.exception("Could not reconcile CPU scaling after startup")
            self.finished.set()

    def status(self):
        return {"ready": self.ready, "failed": self.failed,
                "stage": "ready" if self.ready else "failed" if self.failed
                else self.controller.startup_phase,
                "elapsed_seconds": round(time.monotonic() - self.started)}

    async def handle(self, request):
        path = request.path.removeprefix("/api") if request.path.startswith("/api/") else request.path
        if path == "/split/startup" and request.method in {"GET", "HEAD"}:
            return web.json_response(self.status(), headers=NO_CACHE)
        if not self.ready and request.method in {"GET", "HEAD"} and path in {"/", "/index.html"}:
            return web.Response(text=PAGE, content_type="text/html", headers={**NO_CACHE,
                "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'"})
        if not self.finished.is_set():
            # Existing API clients can complete a short cold start in one request.
            # A timed-out or disconnected request never cancels initialization.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.finished.wait(), self.request_wait)
        if not self.ready:
            return web.json_response({"error": "ComfyUIの起動に失敗しました。" if self.failed
                                      else "ComfyUIを準備しています。しばらくして再接続してください。",
                                      "startup": self.status()}, status=503,
                                     headers={**NO_CACHE, "Retry-After": "2"})
        return await self.controller.handle(request)

    async def close(self, app):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        await self.controller.close(app)
