import asyncio
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from ambient.api import JOB_BODY_LIMIT
from ambient.contracts import validate_request
from ambient.comfy import workflow
from ambient.media import finalize
from ambient.service import JobService, Conflict
from ambient_fixtures import object_info


def request(**patch):
    return {
        "requestId": str(uuid4()),
        "mode": "h3",
        "prompt": "A quiet room",
        "sound": "Soft breeze",
        "seed": 42,
        "resolution": "preview",
        **patch,
    }


class Store(dict):
    def put(self, key, value, skip_if_exists=False):
        if skip_if_exists and key in self:
            return False
        self[key] = value
        return True


class Volume:
    def __init__(self):
        self.files = {}

    def batch_upload(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def put_file(self, source, target):
        self.files[target] = source.read()

    def read_file_into_fileobj(self, source, target):
        target.write(self.files[source])


class ContractsTest(unittest.TestCase):
    def test_idempotency_conflict_and_cancel_race(self):
        store = Store()
        calls = []
        service = JobService(store, lambda key: calls.append(key) or "fc-1")
        req = request()
        self.assertEqual(service.submit(req)["id"], service.submit(req)["id"])
        self.assertEqual(len(calls), 1)
        with self.assertRaises(Conflict):
            service.submit({**req, "seed": 43})
        service.cancel(req["requestId"])
        store[req["requestId"]]["status"] = (
            "completed"  # late worker write cannot undo cancellation
        )
        self.assertEqual(service.get(req["requestId"])["status"], "cancelled")

    def test_unconfirmed_dispatch_is_pending_but_worker_timeout_is_failed(self):
        store = Store()
        service = JobService(store, lambda _: "fc-1", now=lambda: 500)
        req = request()
        service.submit(req)
        store[req["requestId"]]["createdAt"] = 0
        del store["call:" + req["requestId"]]
        pending = service.get(req["requestId"])
        self.assertEqual(pending["status"], "queued")
        self.assertIn("Dispatch unconfirmed", pending["stage"])
        self.assertNotIn("error", pending)
        self.assertEqual(store[req["requestId"]]["stage"], "Queued")
        store["call:" + req["requestId"]] = "fc-2"
        service.reconcile = lambda _: "Worker timed out"
        self.assertEqual(service.get(req["requestId"])["status"], "failed")

    def test_cancel_preserves_terminal_jobs_and_is_idempotent(self):
        for status in ("completed", "failed", "cancelled"):
            with self.subTest(status=status):
                store = Store()
                service = JobService(store, lambda _: "fc-1")
                job_id = request()["requestId"]
                store[job_id] = {"id": job_id, "status": status, "stage": status}
                if status == "completed":
                    store[job_id]["clip"] = {"id": job_id}
                before = service.get(job_id)
                self.assertEqual(service.cancel(job_id), before)
                self.assertEqual(service.cancel(job_id), before)
                self.assertNotIn("cancel:" + job_id, store)

    def test_fast_has_no_image_input(self):
        with self.assertRaises(ValueError):
            validate_request({**request(), "mode": "fasth3", "imageId": str(uuid4())})
        for bad in [True, -1, 2**32, 1.1]:
            with self.assertRaises(ValueError):
                validate_request({**request(), "seed": bad})
        with self.assertRaises(ValueError):
            validate_request({**request(), "requestId": "../escape"})
        with self.assertRaises(ValueError):
            validate_request({**request(), "resolution": []})

    def test_native_audio_turbo_and_anchor(self):
        req = request()
        graph = workflow(req, "ambient/anchor.png", object_info=object_info())
        self.assertEqual(graph["6"]["inputs"]["first_frame"], ["16", 0])
        self.assertEqual(graph["10"]["inputs"]["steps"], 8)
        self.assertEqual(graph["13"]["class_type"], "VAEDecodeAudio")
        self.assertEqual(graph["14"]["inputs"]["audio"], ["13", 0])
        self.assertTrue(graph["15"]["inputs"]["filename_prefix"].startswith("ambient/raw/"))


class ApiTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from ambient.api import create_api

        self.store = Store()
        self.inputs = Volume()
        self.outputs = Volume()
        self.service = JobService(self.store, lambda _: "fc-1")
        self.client = TestClient(
            create_api(
                self.service,
                lambda: {"h3": {"ready": True}, "fasth3": {"ready": True}},
                self.inputs,
                self.outputs,
            )
        )
        self.addCleanup(self.client.close)

    def test_upload_and_clip_range(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (40, 20)).save(image, format="PNG")
        response = self.client.post(
            "/images", files={"image": ("test.png", image.getvalue(), "image/png")}
        )
        self.assertEqual(response.status_code, 201, response.text)
        asset = response.json()["id"]
        self.assertIn(f"ambient/images/{asset}.png", self.inputs.files)
        req = request()
        req["imageId"] = asset
        self.assertEqual(self.client.post("/jobs", json=req).status_code, 202)
        self.assertEqual(self.client.post("/jobs", json=req).json()["id"], req["requestId"])
        self.assertEqual(self.client.get("/clips/" + req["requestId"]).status_code, 404)
        self.store[req["requestId"]]["status"] = "completed"
        self.outputs.files[f"ambient/clips/{req['requestId']}.mp4"] = b"0123456789"
        response = self.client.get("/clips/" + req["requestId"], headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        cancelled = self.client.delete("/jobs/" + req["requestId"])
        self.assertEqual(cancelled.json()["status"], "completed")
        self.assertEqual(self.client.get("/clips/" + req["requestId"]).content, b"0123456789")
        child = request(parentClipId=req["requestId"])
        self.assertEqual(self.client.post("/jobs", json=child).status_code, 202)

    def test_cancel_pending_job_hides_a_late_clip(self):
        req = request()
        self.service.submit(req)
        job_id = req["requestId"]
        self.assertEqual(self.client.delete("/jobs/" + job_id).json()["status"], "cancelled")
        self.store[job_id].update(status="completed", clip={"id": job_id})
        self.outputs.files[f"ambient/clips/{job_id}.mp4"] = b"late video"
        self.assertEqual(self.client.get("/jobs/" + job_id).json()["status"], "cancelled")
        self.assertEqual(self.client.get("/clips/" + job_id).status_code, 404)

    def test_bad_requests(self):
        self.assertEqual(self.client.post("/jobs", json={}).status_code, 400)
        self.assertEqual(self.client.get("/jobs/" + str(uuid4())).status_code, 404)
        self.assertEqual(
            self.client.post(
                "/images", files={"image": ("bad.png", b"bad", "image/png")}
            ).status_code,
            400,
        )

    def test_job_body_size_boundary_and_maximum_text(self):
        req = request(prompt="景" * 8000, sound="音" * 4000)
        encoded = json.dumps(req).encode()
        body = encoded + b" " * (JOB_BODY_LIMIT - len(encoded))
        with patch.object(self.service, "submit", wraps=self.service.submit) as submit:
            response = self.client.post(
                "/jobs", content=body + b" ", headers={"Content-Type": "application/json"}
            )
            self.assertEqual(response.status_code, 413)
            submit.assert_not_called()
            self.assertEqual(self.store, {})
            response = self.client.post(
                "/jobs", content=body, headers={"Content-Type": "application/json"}
            )
            self.assertEqual(response.status_code, 202, response.text)
            submit.assert_called_once()

    def test_oversized_job_stream_stops_before_json_parsing_or_dispatch(self):
        async def exercise():
            from unittest.mock import AsyncMock

            receive = AsyncMock(side_effect=[
                {"type": "http.request", "body": b" " * JOB_BODY_LIMIT, "more_body": True},
                {"type": "http.request", "body": b" ", "more_body": True},
                AssertionError("Read past the body limit"),
            ])
            send = AsyncMock()
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": "/jobs",
                "query_string": b"", "headers": [(b"content-type", b"application/json")],
                "server": ("testserver", 80), "client": ("testclient", 123),
            }
            await self.client.app(scope, receive, send)
            self.assertEqual(receive.await_count, 2)
            self.assertEqual(send.await_args_list[0].args[0]["status"], 413)

        asyncio.run(exercise())
        self.assertEqual(self.store, {})

    def test_parent_validation_and_conflicting_retry(self):
        parent = request()
        self.service.submit(parent)
        child = request(parentClipId=parent["requestId"])
        self.assertEqual(self.client.post("/jobs", json=child).status_code, 400)
        self.store[parent["requestId"]]["status"] = "completed"
        self.assertEqual(self.client.post("/jobs", json=child).status_code, 202)
        self.assertEqual(self.client.post("/jobs", json={**child, "seed": 43}).status_code, 409)

    def test_mode_lookup_runs_outside_the_event_loop(self):
        from fastapi.testclient import TestClient
        from ambient.api import create_api

        def modes():
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return {"h3": {"ready": False, "reason": "Not prepared"}}

        with TestClient(create_api(self.service, modes, self.inputs, self.outputs)) as client:
            response = client.post("/jobs", json=request())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "Not prepared"})
        self.assertEqual(self.store, {})


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class MediaTest(unittest.TestCase):
    def test_native_audio_and_exact_last_frame(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "source.mp4"
            out = root / "out.mp4"
            frame = root / "last.png"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=128x96:rate=24:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=220:duration=1",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    str(source),
                ],
                check=True,
            )
            clip = finalize(source, out, frame, str(uuid4()))
            self.assertTrue(clip["hasAudio"])
            self.assertEqual(clip["frames"], 24)
            with Image.open(frame) as last:
                self.assertEqual(last.size, (128, 96))
            self.assertTrue(out.exists())
            self.assertFalse(out.with_suffix(".part.mp4").exists())
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-an",
                    "-c:v",
                    "copy",
                    str(root / "silent.mp4"),
                ],
                check=True,
            )
            with self.assertRaises(RuntimeError):
                finalize(root / "silent.mp4", root / "bad.mp4", root / "bad.png", str(uuid4()))
            self.assertFalse((root / "bad.part.mp4").exists())


class MediaFailureTest(unittest.TestCase):
    def test_failed_finalize_removes_partial_output(self):
        for failure in ("encode", "probe", "extract", "metadata"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                out, frame = root / "out.mp4", root / "last.png"

                def run(args):
                    Path(args[-1]).write_bytes(b"partial artifact")
                    if failure == "encode" or (failure == "extract" and args[-1] == str(frame)):
                        raise RuntimeError(failure)

                info = {
                    "streams": [
                        {
                            "codec_type": "video",
                            "nb_read_frames": "2",
                            "duration": "1",
                            "avg_frame_rate": "24/1",
                            "width": 128,
                            "height": 96,
                        },
                        {"codec_type": "audio", "duration": "2" if failure == "metadata" else "1"},
                    ]
                }
                with (
                    patch("ambient.media.run", side_effect=run),
                    patch(
                        "ambient.media.subprocess.check_output",
                        side_effect=RuntimeError("probe") if failure == "probe" else None,
                        return_value=json.dumps(info),
                    ),
                ):
                    with self.assertRaises(RuntimeError):
                        finalize(root / "source.mp4", out, frame, str(uuid4()))
                self.assertFalse(out.with_suffix(".part.mp4").exists())
                self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
