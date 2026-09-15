import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

from ambient.comfy import generate
from ambient import client as ambient_client
from ambient.readiness import check_comfyui
from ambient.urls import validate_endpoint
from scripts import ambient_smoke
from test_ambient import request
from ambient_fixtures import object_info


class EndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_endpoints_are_rejected_before_creating_a_session(self):
        headers = {"Modal-Key": "test-key", "Modal-Secret": "test-secret"}
        for url in (
            "http://example.com",
            "http://127.0.0.1",
            "https:///missing",
            "https://host:bad",
            "https://user:secret@host",
            "https://host?query=1",
            "https://host/#fragment",
            "https://bad host",
            "https://host\n",
        ):
            with self.subTest(url=url), patch("aiohttp.ClientSession") as client:
                with self.assertRaises(ValueError):
                    await check_comfyui(url, headers)
                with self.assertRaises(ValueError):
                    await generate(
                        url,
                        headers,
                        request(),
                        None,
                        Path("/unused"),
                        lambda: False,
                        lambda _: None,
                    )
                client.assert_not_called()

    async def test_https_hosts_and_only_explicit_loopback_http_are_allowed(self):
        self.assertEqual(
            validate_endpoint("https://comfy.example:443/prefix/"),
            "https://comfy.example:443/prefix",
        )
        for host in ("localhost", "127.0.0.1", "[::1]"):
            url = f"http://{host}:8188"
            self.assertEqual(validate_endpoint(url, allow_http_loopback=True), url)
            with self.assertRaises(ValueError):
                validate_endpoint(url)
        with self.assertRaises(ValueError):
            validate_endpoint("http://comfy.example", allow_http_loopback=True)

    async def test_smoke_rejects_http_before_opening_network_or_creating_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with (
                patch.dict(os.environ, {"AMBIENT_BACKEND_URL": "http://example.com"}),
                patch.object(
                    sys, "argv", ["ambient_smoke.py", "--mode", "h3", "--output", str(output)]
                ),
                patch.object(ambient_client, "build_opener") as opener,
            ):
                with self.assertRaises(ValueError):
                    ambient_smoke.main()
                opener.assert_not_called()
            self.assertFalse(output.exists())

    async def test_smoke_does_not_forward_credentials_on_redirect(self):
        with self.assertRaises(ValueError):
            ambient_client.NoRedirect().redirect_request(
                None, None, 302, "", {}, "http://example.com"
            )


class DownloadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name) / "generated.mp4"
        self.chunks = 8
        self.delay = 0.05
        self.redirect = False
        self.redirect_phase = "view"
        self.redirect_requests = 0
        self.objects = object_info()
        self.prompts = []
        self.cancels = []
        self.cancel_status = 200
        self.catalog_delay = 0
        self.history_delay = 0

        async def objects(req):
            await asyncio.sleep(self.catalog_delay)
            return web.json_response(self.objects)

        async def status(req):
            return web.json_response({"api_version": 1, "mode": "split"})

        async def ws(req):
            if self.redirect and self.redirect_phase == "ws":
                raise web.HTTPFound("/redirected")
            socket = web.WebSocketResponse(compress=False)
            await socket.prepare(req)
            async for _ in socket:
                pass
            return socket

        async def prompt(req):
            if self.redirect and self.redirect_phase == "prompt":
                raise web.HTTPFound("/redirected")
            self.prompts.append(await req.json())
            return web.json_response({"prompt_id": "own"})

        async def history(req):
            await asyncio.sleep(self.history_delay)
            return web.json_response(
                {
                    "own": {
                        "status": {"completed": True},
                        "outputs": {"15": {"images": [{"filename": "video.mp4"}]}},
                    }
                }
            )

        async def cancel(req):
            self.cancels.append(req.match_info["id"])
            return web.json_response({}, status=self.cancel_status)

        async def view(req):
            if self.redirect:
                raise web.HTTPFound("/redirected")
            response = web.StreamResponse()
            await response.prepare(req)
            try:
                for _ in range(self.chunks):
                    await asyncio.sleep(self.delay)
                    await response.write(b"video")
                await response.write_eof()
            except ConnectionResetError:
                pass  # The timeout cases deliberately close the client connection.
            return response

        async def redirected(req):
            self.redirect_requests += 1
            return web.Response(body=b"unexpected redirect")

        app = web.Application()
        app.router.add_get("/modal-control/v1/status", status)
        app.router.add_get("/ws", ws)
        app.router.add_post("/prompt", prompt)
        app.router.add_get("/object_info", objects)
        app.router.add_get("/history/{id}", history)
        app.router.add_post("/jobs/{id}/cancel", cancel)
        app.router.add_get("/view", view)
        app.router.add_route("*", "/redirected", redirected)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def generate(self, timeout=3, control_timeout=0.25, read_timeout=0.5):
        real_timeout = aiohttp.ClientTimeout

        def scaled_timeout(*, total=None, **kwargs):
            # Exercise seconds-long production policies in fractions of a second.
            if total == 120:
                total = control_timeout
            if kwargs.get("sock_read") == 120:
                kwargs["sock_read"] = read_timeout
            return real_timeout(total=total, **kwargs)

        with patch("aiohttp.ClientTimeout", side_effect=scaled_timeout):
            await generate(
                self.base,
                {},
                request(),
                None,
                self.destination,
                lambda: False,
                lambda _: None,
                timeout=timeout,
            )

    async def test_progressing_download_can_outlast_control_request_timeout(self):
        await self.generate()
        self.assertEqual(self.destination.read_bytes(), b"video" * self.chunks)
        self.assertEqual(self.cancels, [])

    async def test_generation_uses_current_server_definitions(self):
        self.objects["MiniMaxH3ImageToVideo"]["output"] = ["LATENT", "CONDITIONING"]
        await self.generate()
        self.assertEqual(self.prompts[0]["prompt"]["7"]["inputs"]["conditioning"], ["6", 1])

    async def test_incompatible_server_never_receives_a_prompt(self):
        del self.objects["MiniMaxH3ImageToVideo"]
        with self.assertRaisesRegex(ValueError, "missing native node"):
            await self.generate()
        self.assertEqual(self.prompts, [])

    async def test_stalled_download_times_out(self):
        self.delay = 0.2
        with self.assertRaises(TimeoutError):
            await self.generate(read_timeout=0.03)
        self.assertEqual(self.cancels, ["own"])

    async def test_job_deadline_still_bounds_a_progressing_download(self):
        with self.assertRaises(TimeoutError):
            await self.generate(timeout=0.05)
        self.assertEqual(self.cancels, ["own"])

    async def test_history_timeout_cancels_only_submitted_job(self):
        self.history_delay = 0.1
        with self.assertRaisesRegex(TimeoutError, "cancellation requested"):
            await self.generate(control_timeout=0.03)
        self.assertEqual(self.cancels, ["own"])

    async def test_timeout_before_submission_does_not_cancel_any_job(self):
        self.catalog_delay = 0.1
        with self.assertRaises(TimeoutError):
            await self.generate(control_timeout=0.03)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.cancels, [])

    async def test_failed_timeout_cancellation_is_reported_as_unconfirmed(self):
        self.cancel_status = 503
        with self.assertRaisesRegex(TimeoutError, "cancellation could not be confirmed"):
            await self.generate(timeout=0)
        self.assertEqual(self.cancels, ["own"])

    async def test_redirects_are_rejected_before_any_followup_request(self):
        self.redirect = True
        for phase in ("ws", "prompt", "view"):
            with self.subTest(phase=phase):
                self.redirect_phase = phase
                with self.assertRaisesRegex(ValueError, "redirects are not allowed"):
                    await self.generate()
                self.assertEqual(self.redirect_requests, 0)


if __name__ == "__main__":
    unittest.main()
