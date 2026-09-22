import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from ambient.api import create_api
from ambient.contracts import public_job
from ambient.processing import run_job
from ambient.service import JobService
from test_ambient import Store, request


class WorkflowConnectionTests(unittest.TestCase):
    def call(self, *, status=200, data=None, error=None):
        response = MagicMock(status=status, headers={"Retry-After": "3"})
        response.json = AsyncMock(return_value=data)
        connection = MagicMock()
        connection.__aenter__ = AsyncMock(return_value=response, side_effect=error)
        connection.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get.return_value = connection
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with patch.dict("os.environ", {"AMBIENT_COMFYUI_URL": "https://comfy.example.test"}), \
                patch("aiohttp.ClientSession", return_value=session), \
                TestClient(create_api(None, None, None, None)) as client:
            return client.get("/workflows")

    def test_cold_start_timeout_returns_retryable_json_without_submitting_work(self):
        response = self.call(error=asyncio.TimeoutError())
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()["retryable"])
        self.assertEqual(response.headers["Retry-After"], "2")

    def test_startup_state_and_retry_after_survive_proxy(self):
        response = self.call(status=503, data={"error": "Starting", "startup": {"stage": "starting_comfy"}})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["startup"]["stage"], "starting_comfy")
        self.assertEqual(response.headers["Retry-After"], "3")

    def test_invalid_credentials_are_not_indefinitely_retried(self):
        response = self.call(status=401)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["retryable"])


class ProgressPublicationTests(unittest.TestCase):
    def test_real_sampling_counts_are_visible_then_cleared_when_video_is_saved(self):
        jobs = Store()
        service = JobService(jobs, lambda _: None)
        req = request()
        service.submit(req)
        observed = []

        def generate(request, image, source, cancelled, progress):
            progress("Sampling", {"value": 3, "max": 8})
            observed.append(service.get(req["requestId"]))

        def publish(source, id):
            observed.append(service.get(id))
            return {"id": id, "hasAudio": True}

        storage = SimpleNamespace(prepare_anchor=lambda *_: None, publish_clip=publish)
        run_job(req["requestId"], jobs, storage, {("h3", "comfyui"): generate})
        self.assertEqual(observed[0]["progress"], {"value": 3, "max": 8})
        self.assertIsNone(observed[1]["progress"])
        final = public_job(jobs.get("terminal:" + req["requestId"]))
        self.assertEqual(final["status"], "completed")
        self.assertIsNone(final["progress"])
