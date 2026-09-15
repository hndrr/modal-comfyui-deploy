"""Run the actual CLI/client/API/processor together with local generation doubles."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from PIL import Image

from ambient.api import create_api
from ambient.cli import main
from ambient.client import Client
from ambient.contracts import ROUTES
from ambient.models import references
from ambient.processing import run_job
from ambient.service import JobService
from test_ambient import Store, Volume, request


class ASGIOpener:
    """Pass urllib requests to the actual local FastAPI application."""

    def __init__(self, api):
        self.api, self.requests = api, []
        self.drop_response = False

    def open(self, req, timeout):
        path = urlsplit(req.full_url).path
        self.requests.append((req.get_method(), path, req.data))
        response = self.api.request(
            req.get_method(), path, content=req.data, headers=dict(req.header_items())
        )
        if self.drop_response and req.get_method() == "POST" and path == "/jobs":
            self.drop_response = False
            raise OSError("response lost after acceptance")
        body = io.BytesIO(response.content)
        if response.status_code >= 400:
            raise HTTPError(
                req.full_url, response.status_code, "error", response.headers, body
            )
        return body


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store, self.inputs, self.outputs = Store(), Volume(), Volume()
        self.invocations = []
        self.run_immediately = True
        self.modes = {
            "h3": {"ready": True, "backends": {"comfyui": {"ready": True}}},
            "fasth3": {
                "ready": True,
                "backends": {
                    "comfyui": {"ready": True},
                    "fastvideo": {"ready": True},
                },
            },
        }

        def generate(req, image, source, cancelled, progress):
            self.invocations.append((req["mode"], req["backend"]))
            progress("Mock generation")
            source.write_bytes(f"mock video {req['mode']} {req['backend']}".encode())

        def publish(source, job_id):
            self.outputs.files[f"ambient/clips/{job_id}.mp4"] = source.read_bytes()
            return {"id": job_id, "hasAudio": True}

        storage = SimpleNamespace(
            prepare_anchor=lambda req, directory: None, publish_clip=publish
        )

        def spawn(job_id):
            if self.run_immediately:
                run_job(
                    job_id, self.store, storage, {route: generate for route in ROUTES}
                )
            return "fc-mock"

        self.service = JobService(
            self.store,
            spawn,
            reference=lambda req: references(req["mode"], req["backend"]),
        )
        api = TestClient(
            create_api(self.service, lambda: self.modes, self.inputs, self.outputs)
        )
        self.addCleanup(api.close)
        self.opener = ASGIOpener(api)
        self.client = Client("https://ambient.test", {}, self.opener)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args), client=self.client)
        return code, out.getvalue(), err.getvalue()

    def generate(self, mode="fasth3", backend="comfyui", *extra):
        return self.invoke(
            "generate",
            "--mode",
            mode,
            "--backend",
            backend,
            "--prompt",
            "A quiet room",
            "--sound",
            "Soft breeze",
            "--output",
            str(self.root),
            *extra,
        )

    def test_three_routes_generate_save_retrieve_and_report_references(self):
        self.assertEqual(self.invoke("capabilities")[0], 0)
        self.assertEqual(self.invocations, [])
        for mode, backend in ROUTES:
            with self.subTest(mode=mode, backend=backend):
                code, out, err = self.generate(mode, backend)
                self.assertEqual(code, 0, err)
                result = json.loads(out)
                job = result["job"]
                job_id = job["id"]
                self.assertEqual((job["mode"], job["backend"]), (mode, backend))
                self.assertIn("models", job["references"])
                self.assertEqual(
                    Path(result["path"]).read_bytes(),
                    f"mock video {mode} {backend}".encode(),
                )
                saved = self.root / f"{job_id}.request.json"
                self.assertEqual(json.loads(saved.read_text())["backend"], backend)
                self.assertEqual(self.invoke("status", job_id)[0], 0)
                self.assertEqual(self.invoke("cancel", job_id)[0], 0)
                self.assertEqual(self.service.get(job_id)["status"], "completed")
                Path(result["path"]).unlink()
                self.assertEqual(
                    self.invoke("download", job_id, "--output", str(self.root))[0], 0
                )
                self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(self.invocations, list(ROUTES))

    def test_lost_post_response_can_be_retried_without_a_second_generation(self):
        self.opener.drop_response = True
        code, _, err = self.generate()
        self.assertEqual(code, 1)
        self.assertIn("response lost", err)
        saved = next(self.root.glob("*.request.json"))
        job_id = json.loads(saved.read_text())["requestId"]
        self.assertIn(job_id, err)
        self.assertEqual(self.invoke("status", job_id)[0], 0)
        # Even unavailable preparation must not stop retrieval of an accepted job.
        self.modes["fasth3"]["backends"]["comfyui"] = {
            "ready": False,
            "reason": "Changed preparation",
        }
        code, _, err = self.invoke("submit", str(saved), "--output", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.invocations, [("fasth3", "comfyui")])
        posts = [
            body
            for method, path, body in self.opener.requests
            if method == "POST" and path == "/jobs"
        ]
        self.assertEqual(posts[0], posts[1])
        changed = self.root / "changed.json"
        changed.write_text(
            json.dumps({**json.loads(saved.read_text()), "backend": "fastvideo"})
        )
        code, _, err = self.invoke(
            "submit", str(changed), "--output", str(self.root / "other")
        )
        self.assertEqual(code, 1)
        self.assertIn("409", err)

    def test_route_readiness_does_not_use_legacy_default_ready_flag(self):
        self.modes["fasth3"]["ready"] = False
        self.modes["fasth3"]["backends"]["fastvideo"] = {
            "ready": False,
            "reason": "No FastVideo snapshot",
        }
        self.assertEqual(self.generate()[0], 0)
        code, _, err = self.generate("fasth3", "fastvideo")
        self.assertEqual(code, 1)
        self.assertIn("503", err)
        self.assertEqual(self.invocations, [("fasth3", "comfyui")])

    def test_image_upload_and_parent_are_h3_only(self):
        path = self.root / "image.png"
        Image.new("RGB", (8, 8)).save(path)
        code, out, err = self.generate("h3", "comfyui", "--image", str(path))
        self.assertEqual(code, 0, err)
        parent = json.loads(out)["job"]["id"]
        req = self.store[parent]["request"]
        self.assertIn("imageId", req)
        self.assertIn(f"ambient/images/{req['imageId']}.png", self.inputs.files)
        self.assertEqual(
            self.generate("h3", "comfyui", "--parent-clip-id", parent)[0], 0
        )
        before = len(self.opener.requests)
        self.assertEqual(self.generate("fasth3", "comfyui", "--image", str(path))[0], 1)
        self.assertEqual(self.generate("h3", "fastvideo")[0], 1)
        self.assertEqual(len(self.opener.requests), before)

    def test_ctrl_c_requests_scoped_cancel_and_timeout_does_not_resubmit(self):
        self.run_immediately = False
        with patch.object(self.client, "wait", side_effect=KeyboardInterrupt):
            code, _, err = self.generate()
        self.assertEqual(code, 130)
        self.assertIn("Cancel response", err)
        job_id = next(
            key for key in self.store if not key.startswith(("call:", "cancel:"))
        )
        self.assertEqual(self.service.get(job_id)["status"], "cancelled")
        with patch.object(
            self.client, "wait", side_effect=TimeoutError("Inspect job timeout")
        ):
            code, _, err = self.generate()
        self.assertEqual(code, 1)
        self.assertIn("Inspect job", err)
        posts = [r for r in self.opener.requests if r[:2] == ("POST", "/jobs")]
        self.assertEqual(len(posts), 2)

    def test_download_failure_does_not_publish_partial_or_destroy_existing_clip(self):
        job_id = request()["requestId"]
        target = self.root / f"{job_id}.mp4"
        target.write_bytes(b"old good clip")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [b"partial", OSError("transfer failed")]
        with (
            patch.object(self.client, "open", return_value=response),
            self.assertRaises(OSError),
        ):
            self.client.download(
                job_id, self.root, {"id": job_id, "status": "completed"}
            )
        self.assertEqual(target.read_bytes(), b"old good clip")
        self.assertFalse(list(self.root.glob("*.part.mp4")))

    def test_short_clean_eof_does_not_replace_a_completed_download(self):
        job_id = request()["requestId"]
        target = self.root / f"{job_id}.mp4"
        target.write_bytes(b"old good clip")
        with (
            patch.object(self.client, "open", return_value=io.BytesIO(b"truncated")),
            self.assertRaisesRegex(RuntimeError, "Incomplete clip"),
        ):
            self.client.download(
                job_id,
                self.root,
                {
                    "id": job_id,
                    "status": "completed",
                    "clip": {"bytes": 100},
                },
            )
        self.assertEqual(target.read_bytes(), b"old good clip")
        self.assertFalse(list(self.root.glob("*.part.mp4")))


if __name__ == "__main__":
    unittest.main()
