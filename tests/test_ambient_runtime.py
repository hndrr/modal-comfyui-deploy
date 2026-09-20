import copy
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from PIL import Image

from ambient.config import RETENTION_SECONDS
from ambient.maintenance import cleanup_jobs
from ambient.processing import run_job
from ambient.contracts import RESOLUTIONS, ROUTES
from ambient.readiness import describe_modes
from ambient.models import references
from ambient.service import JobService
from ambient.storage import AmbientStorage, clip_path, frame_path, image_path


def request(**changes):
    return {
        "requestId": str(uuid4()),
        "mode": "h3",
        "prompt": "A quiet room",
        "sound": "Soft breeze",
        "seed": 42,
        "resolution": "preview",
        **changes,
    }


class Store(dict):
    """Copy records at the boundary, as a remote Dict does."""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def put(self, key, value, skip_if_exists=False):
        if skip_if_exists and key in self:
            return False
        self[key] = copy.deepcopy(value)
        if isinstance(value, dict) and "status" in value:
            self.events.append(value["status"])
        return True


class Volume:
    def __init__(self, name, events):
        self.name = name
        self.events = events
        self.files = {}

    def read_file_into_fileobj(self, source, target):
        target.write(self.files[source])

    def commit(self):
        self.events.append(self.name + ":commit")

    def reload(self):
        self.events.append(self.name + ":reload")


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.events = []
        self.jobs = Store(self.events)
        self.service = JobService(self.jobs, lambda _: "fc-1")
        self.inputs = Volume("inputs", self.events)
        self.outputs = Volume("outputs", self.events)
        self.storage = AmbientStorage(
            self.inputs, self.outputs, self.root / "inputs", self.root / "outputs"
        )

    def submit(self, **changes):
        req = request(**changes)
        self.service.submit(req)
        self.events.clear()
        return req["requestId"]

    def generate(self, req, image, source, cancelled, progress):
        self.assertFalse(cancelled())
        source.write_bytes(b"native-video")

    def encode(self, source, target, frame, job_id):
        self.assertEqual(source.read_bytes(), b"native-video")
        for path in (target, frame):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"artifact")
        return {"id": job_id, "hasAudio": True}

    def run_job(self, job_id, generator=None):
        generator = generator or self.generate
        run_job(job_id, self.jobs, self.storage, {route: generator for route in ROUTES})

    def test_both_modes_publish_only_after_both_volumes_commit(self):
        for mode, backend in ROUTES:
            with (
                self.subTest(mode=mode),
                patch("ambient.storage.finalize", side_effect=self.encode),
            ):
                job_id = self.submit(mode=mode, backend=backend,
                                     **({"sessionId": request()["requestId"], "workflowRevision": 0} if mode == "fasth3-8step-i2v" else {}))
                selected = Mock(side_effect=self.generate)
                unused = Mock(side_effect=AssertionError("Wrong generation backend"))
                generators = {**{route: unused for route in ROUTES}, (mode, backend): selected}
                run_job(job_id, self.jobs, self.storage, generators)
                selected.assert_called_once()
                unused.assert_not_called()
                self.assertEqual(self.service.get(job_id)["clip"], {"id": job_id, "hasAudio": True})
                self.assertLess(self.events.index("inputs:commit"), self.events.index("completed"))
                self.assertLess(self.events.index("outputs:commit"), self.events.index("completed"))
                self.assertFalse(
                    selected.call_args.args[2].parent.exists(), "Temporary files leaked"
                )

    def test_cancel_before_start_never_generates(self):
        job_id = self.submit()
        self.service.cancel(job_id)
        generator = Mock()
        with patch("ambient.storage.finalize") as encode:
            self.run_job(job_id, generator)
        generator.assert_not_called()
        encode.assert_not_called()
        self.assertEqual(self.service.get(job_id)["status"], "cancelled")

    def test_unconfirmed_dispatch_can_complete_without_resubmission(self):
        job_id = self.submit()
        self.jobs[job_id]["createdAt"] = time.time() - 301
        self.jobs.pop("call:" + job_id)
        pending = self.service.get(job_id)
        self.assertEqual(pending["status"], "queued")
        with patch.object(self.service, "spawn") as spawn:
            self.assertEqual(self.service.submit(self.jobs[job_id]["request"]), pending)
            spawn.assert_not_called()
        with patch("ambient.storage.finalize", side_effect=self.encode):
            self.run_job(job_id)
        result = self.service.get(job_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["clip"]["id"], job_id)
        self.assertNotIn("error", result)

    def test_retired_fastvideo_jobs_never_dispatch_to_comfyui(self):
        for explicit_backend in (False, True):
            with self.subTest(explicit_backend=explicit_backend):
                req = request(mode="fasth3")
                if explicit_backend:
                    req["backend"] = "fastvideo"
                job_id = req["requestId"]
                self.jobs.put(job_id, {
                    "id": job_id, "status": "queued", "request": req,
                    "createdAt": time.time(),
                })
                generator = Mock()
                with patch.object(self.storage, "prepare_anchor") as prepare, patch("builtins.print"):
                    self.run_job(job_id, generator)
                generator.assert_not_called()
                prepare.assert_not_called()
                result = self.service.get(job_id)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["backend"], "fastvideo")
                self.assertIn("Unsupported", result["error"])

    def test_cancel_during_generation_discards_result(self):
        job_id = self.submit()

        def generate(*args):
            self.generate(*args)
            self.service.cancel(job_id)

        with patch("ambient.storage.finalize") as encode:
            self.run_job(job_id, generate)
        encode.assert_not_called()
        self.assertNotIn("completed", self.events)
        self.assertNotIn("clip", self.service.get(job_id))

    def test_cancel_during_commit_does_not_publish_completion(self):
        job_id = self.submit()

        def commit():
            self.events.append("inputs:commit")
            self.service.cancel(job_id)

        with (
            patch("ambient.storage.finalize", side_effect=self.encode),
            patch.object(self.inputs, "commit", commit),
        ):
            self.run_job(job_id)
        self.assertIn("outputs:commit", self.events)
        self.assertNotIn("completed", self.events)
        self.assertEqual(self.service.get(job_id)["status"], "cancelled")

    def test_generation_and_encoding_failures_are_reported(self):
        for failure in ("generation", "encoding"):
            with self.subTest(failure=failure):
                job_id = self.submit()
                generator = (
                    Mock(side_effect=RuntimeError(failure))
                    if failure == "generation"
                    else self.generate
                )
                with (
                    patch("ambient.storage.finalize", side_effect=RuntimeError("encoding")),
                    patch("builtins.print"),
                ):
                    self.run_job(job_id, generator)
                self.assertEqual(self.service.get(job_id)["status"], "failed")
                self.assertEqual(self.service.get(job_id)["error"], failure)
                self.assertNotIn("completed", self.events)
                self.assertNotIn("inputs:commit", self.events)

    def test_commit_failure_never_reports_completed(self):
        for volume in (self.inputs, self.outputs):
            with self.subTest(volume=volume.name):
                job_id = self.submit()
                with (
                    patch("ambient.storage.finalize", side_effect=self.encode),
                    patch.object(volume, "commit", side_effect=RuntimeError("commit failed")),
                    patch("builtins.print"),
                ):
                    self.run_job(job_id)
                self.assertEqual(self.service.get(job_id)["status"], "failed")
                self.assertEqual(self.service.get(job_id)["error"], "commit failed")
                self.assertNotIn("completed", self.events)

    def test_uploaded_images_and_parent_frames_are_resolved_and_fitted(self):
        source = io.BytesIO()
        Image.new("RGB", (40, 20), "red").save(source, format="PNG")
        for field, path in (("imageId", image_path), ("parentClipId", frame_path)):
            with self.subTest(field=field):
                asset_id = str(uuid4())
                self.inputs.files[path(asset_id)] = source.getvalue()
                req = request(**{field: asset_id})
                anchor = self.storage.prepare_anchor(req, self.root)
                with Image.open(anchor) as image:
                    self.assertEqual(image.size, RESOLUTIONS["preview"])
                    self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
        self.assertIsNone(self.storage.prepare_anchor(request(), self.root))

    def test_missing_anchor_fails_before_generation(self):
        job_id = self.submit(parentClipId=str(uuid4()))
        generator = Mock()
        with patch("builtins.print"):
            self.run_job(job_id, generator)
        generator.assert_not_called()
        self.assertEqual(self.service.get(job_id)["status"], "failed")

    def test_cleanup_keeps_active_and_recent_jobs_and_other_assets(self):
        now = time.time()
        expired = now - RETENTION_SECONDS - 1
        removed = []
        retained = []
        for status, created, cancelled, terminal in (
            ("completed", expired, False, None),
            ("failed", expired, False, None),
            ("running", expired, True, None),
            ("running", expired, False, None),
            ("queued", expired, False, None),
            ("completed", now, False, None),
            ("running", expired, False, "completed"),
            ("running", expired, False, "failed"),
            ("queued", expired, False, "cancelled"),
            ("running", now, False, "completed"),
        ):
            job_id = str(uuid4())
            self.jobs.put(job_id, {"status": status, "createdAt": created})
            self.jobs.put("call:" + job_id, "fc-1")
            self.jobs.put("fast-call:" + job_id, "fc-2")
            if cancelled:
                self.jobs.put("cancel:" + job_id, True)
            if terminal:
                self.jobs.put("terminal:" + job_id, {"status": terminal, "createdAt": created})
            paths = [
                self.storage.input_root / frame_path(job_id),
                self.storage.output_root / clip_path(job_id),
                (self.storage.output_root / clip_path(job_id)).with_suffix(".part.mp4"),
            ]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"clip")
            (
                removed
                if created == expired and (status in ("completed", "failed") or cancelled or terminal)
                else retained
            ).append((job_id, paths))
        self.jobs["prepared:h3"] = {"url": "https://comfy.example", "checkedAt": expired}
        old_files = [
            self.storage.input_root / "ambient/images/old.png",
            self.storage.input_root / "ambient/uploads/old.png",
            self.storage.output_root / "ambient/raw/old.mp4",
        ]
        keep_files = [
            self.storage.input_root / "shared.png",
            self.storage.output_root / "other/video.mp4",
            self.storage.input_root / "ambient/images/recent.png",
        ]
        for path in old_files + keep_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"file")
            os.utime(path, (now, now) if path.name == "recent.png" else (expired, expired))
        cleanup_jobs(self.jobs, self.storage, now=lambda: now)
        for job_id, paths in removed:
            for prefix in ("", "call:", "fast-call:", "cancel:", "terminal:"):
                self.assertNotIn(prefix + job_id, self.jobs)
            self.assertTrue(all(not path.exists() for path in paths))
        for job_id, paths in retained:
            self.assertIn(job_id, self.jobs)
            self.assertIn("call:" + job_id, self.jobs)
            self.assertTrue(all(path.exists() for path in paths))
        self.assertIn("prepared:h3", self.jobs)
        self.assertTrue(all(not path.exists() for path in old_files))
        self.assertTrue(all(path.exists() for path in keep_files))
        self.assertEqual(self.events[-2:], ["inputs:commit", "outputs:commit"])


class ReadinessTest(unittest.TestCase):
    def test_capabilities_use_matching_preparation_records(self):
        url = "https://comfy.example"
        jobs = {
            "prepared:h3": {"url": url, "backend": "split", "gpuValidated": False},
            "prepared:fasth3:comfyui": {"url": url, "backend": "split",
                "references": references("fasth3", "comfyui"), "gpuValidated": False},
        }
        modes = describe_modes(jobs, url)
        for mode in (modes["h3"], modes["fasth3"]):
            self.assertTrue(mode["ready"])
            self.assertIsNone(mode["reason"])
            self.assertFalse(mode["validation"]["gpuValidated"])
        self.assertTrue(modes["h3"]["continuity"])
        self.assertFalse(modes["fasth3"]["imageInput"])
        self.assertFalse(modes["fasth3-8step-t2v"]["ready"])
        self.assertFalse(modes["fasth3-8step-i2v"]["ready"])
        for setting in ("", url + "/changed"):
            self.assertTrue(
                all(not mode["ready"] for mode in describe_modes(jobs, setting).values())
            )
        self.assertTrue(
            all(not mode["ready"] for mode in describe_modes({}, url).values())
        )

    def test_old_standard_comfyui_preparation_requires_a_split_check(self):
        url = "https://comfy.example"
        modes = describe_modes({"prepared:h3": {"url": url}}, url)
        self.assertFalse(modes["h3"]["ready"])
        self.assertIn("splitapp", modes["h3"]["reason"])


class ApiReconcileTest(unittest.TestCase):
    def setUp(self):
        import ambient_app
        self.app = ambient_app
        self.req = request()
        self.call = Mock(object_id="fc-processor")
        self.jobs = Store([])
        self.enterContext(patch.object(ambient_app, "store", return_value=self.jobs))

    def test_api_reconcile_distinguishes_poll_and_execution_timeouts(self):
        from fastapi.testclient import TestClient

        job_id = self.req["requestId"]
        self.jobs.put(job_id, {
            "id": job_id, "request": self.req, "createdAt": time.time() - 30,
            "status": "running", "stage": "Sampling",
        })
        self.jobs.put("call:" + job_id, "fc-processor")
        with (
            patch.object(self.app.modal.FunctionCall, "from_id", return_value=self.call),
            TestClient(self.app.api.get_raw_f()()) as client,
        ):
            for error in (TimeoutError(), self.app.modal.exception.TimeoutError()):
                with self.subTest(error=type(error).__module__):
                    self.call.get.side_effect = error
                    response = client.get("/jobs/" + job_id)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["status"], "running")
            self.call.get.side_effect = self.app.modal.exception.FunctionTimeoutError()
            result = client.get("/jobs/" + job_id).json()
            self.assertEqual(result["status"], "failed")
            self.assertIn("execution timed out", result["error"])



if __name__ == "__main__":
    unittest.main()
