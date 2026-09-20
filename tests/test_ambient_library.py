from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from ambient.library import Library
from ambient.api import create_api
from ambient.processing import run_job
from ambient.service import JobService
from ambient.tagging import recipe
from ambient.h3 import workflow
from fastapi.testclient import TestClient
from types import SimpleNamespace
from unittest.mock import Mock
from comfy_split.ambient_workflows import WorkflowRegistry, h3_metadata, validate_template
from ambient_fixtures import object_info
from test_ambient import Store, request


class MemoryVolume:
    def __init__(self):
        self.files = {}

    def batch_upload(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def put_file(self, source, target):
        self.files[target] = Path(source).read_bytes()

    def remove_file(self, target):
        self.files.pop(target, None)

    def read_file_into_fileobj(self, source, target):
        target.write(self.files[source])


class LibraryTest(unittest.TestCase):
    def test_api_ranges_and_restarting_service_keep_library_separate_from_job_retention(self):
        records, volume = Store(), MemoryVolume()
        library = Library(records, volume)
        clip = {"id": str(uuid4()), "bytes": 10, "hasAudio": True}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"0123456789")
            library.publish(source, clip)
            jobs = Store()
            service = JobService(jobs, lambda _: None)
            with TestClient(create_api(service, lambda: {}, volume, volume, library=Library(records, volume))) as client:
                self.assertEqual(client.get("/library").json()["count"], 1)
                response = client.get(f"/library/{clip['id']}/video", headers={"Range": "bytes=2-5"})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, b"2345")
                self.assertEqual(client.get(f"/clips/{clip['id']}").status_code, 404)
                self.assertEqual(client.delete(f"/library/{clip['id']}").status_code, 200)
                self.assertEqual(client.get(f"/library/{clip['id']}/video").status_code, 404)

    def test_tag_dispatch_failure_does_not_undo_completed_video(self):
        jobs, volume = Store(), MemoryVolume()
        library = Library(Store(), volume)
        service = JobService(jobs, lambda _: None)
        req = request(saveToLibrary=True)
        service.submit(req)
        def generate(request, image, source, cancelled, progress):
            source.write_bytes(b"video")
            return {"effective": {"prompt": "Edited in ComfyUI", "sound": "Rain"}}
        storage = SimpleNamespace(prepare_anchor=lambda *_: None,
            publish_clip=lambda _source, id: {"id": id, "bytes": 5},
            download_clip=lambda _id, path: path.write_bytes(b"video"))
        run_job(req["requestId"], jobs, storage, {("h3", "comfyui"): generate},
                library=library, tag_dispatch=Mock(side_effect=RuntimeError("Tagger unavailable")))
        self.assertEqual(service.get(req["requestId"])["status"], "completed")
        clip = library.get(req["requestId"])
        self.assertEqual(clip["tagging"]["status"], "failed")
        self.assertEqual(clip["generation"]["effective"]["prompt"], "Edited in ComfyUI")

    def test_duplicate_publication_and_deleted_clip_cannot_be_resurrected_by_tagger(self):
        volume, records = MemoryVolume(), Store()
        library = Library(records, volume)
        clip = {"id": str(uuid4()), "bytes": 4, "hasAudio": False}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"test")
            library.publish(source, clip)
            library.publish(source, clip)
            library.tags(clip["id"], {"status": "completed", "tags": ["scene:forest"]})
            self.assertEqual(library.list()["count"], 1)
            self.assertEqual(library.list()["bytes"], 4)
            library.delete(clip["id"])
            with self.assertRaises(KeyError):
                library.tags(clip["id"], {"status": "completed"})
            self.assertEqual(library.list()["count"], 0)
            self.assertEqual(volume.files, {})


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.objects = object_info()
        self.registry = WorkflowRegistry({})
        self.req = request(sessionId=str(uuid4()), workflowRevision=0)

    def body(self, **patch):
        req = {**self.req, **patch}
        graph = workflow(req, object_info=self.objects)
        return {"prompt": graph, "extra_data": {"ambient": h3_metadata(req, graph)}}

    def test_versions_are_pinned_and_fixed_and_live_inputs_do_not_overwrite_each_other(self):
        original = self.registry.prepare(self.body())
        template = deepcopy(self.registry.describe()["stages"]["h3"])
        template["bindings"]["seed"]["source"] = "workflow"
        template["graph"]["8"]["inputs"]["noise_seed"] = 999
        template["graph"]["10"]["inputs"]["steps"] = 12
        self.registry.apply("h3", template, 0, self.objects)
        before = self.registry.prepare(self.body(seed=3))
        self.assertEqual(before["prompt"]["10"]["inputs"]["steps"], 8)
        after = self.registry.prepare(self.body(seed=3, prompt="Forest", workflowRevision=1))
        self.assertEqual(after["prompt"]["8"]["inputs"]["noise_seed"], 999)
        self.assertEqual(after["extra_data"]["ambient"]["effective"]["prompt"], "Forest")
        self.assertEqual(after["extra_data"]["ambient"]["effective"]["sound"], self.req["sound"])
        self.assertEqual(after["extra_data"]["ambient"]["effective"]["settings"]["10"]["inputs"]["steps"], 12)
        self.assertEqual(original["prompt"]["8"]["inputs"]["noise_seed"], 42)
        with self.assertRaisesRegex(ValueError, "another device"):
            self.registry.apply("h3", template, 0, self.objects)

    def test_deleted_output_broken_connection_and_secret_are_rejected(self):
        self.registry.prepare(self.body())
        baseline = self.registry.describe()["stages"]["h3"]
        for mutate in [lambda t: t["graph"].pop("15"),
                       lambda t: t["graph"]["14"]["inputs"].update(images=["missing", 0]),
                       lambda t: t["graph"]["8"]["inputs"].update(api_key="secret")]:
            template = deepcopy(baseline)
            mutate(template)
            with self.assertRaises(ValueError):
                validate_template(template, baseline, self.objects)

    def test_text_first_template_accepts_later_parent_frame_and_then_text_again(self):
        self.registry.prepare(self.body())
        template = self.registry.describe()["stages"]["h3"]
        self.registry.apply("h3", template, 0, self.objects)
        req = {**self.req, "workflowRevision": 1}
        graph = workflow(req, "ambient/new-anchor.png", object_info=self.objects)
        actual = self.registry.prepare({"prompt": graph, "extra_data": {"ambient": h3_metadata(req, graph)}})
        self.assertEqual(actual["prompt"]["16"]["inputs"]["image"], "ambient/new-anchor.png")
        self.assertEqual(actual["prompt"]["6"]["inputs"]["first_frame"], ["16", 0])
        actual = self.registry.prepare(self.body(workflowRevision=1))
        self.assertNotIn("first_frame", actual["prompt"]["6"]["inputs"])

    def test_history_restore_is_a_new_revision(self):
        self.registry.prepare(self.body())
        template = self.registry.describe()["stages"]["h3"]
        self.registry.apply("h3", template, 0, self.objects)
        changed = deepcopy(template)
        changed["graph"]["10"]["inputs"]["steps"] = 16
        self.registry.apply("h3", changed, 1, self.objects)
        self.registry.apply("h3", self.registry.snapshot(1)["stages"]["h3"], 2, self.objects)
        self.assertEqual(self.registry.snapshot()["revision"], 3)
        self.assertEqual(self.registry.snapshot(2)["stages"]["h3"]["graph"]["10"]["inputs"]["steps"], 16)
        initial = self.registry.describe(0)["stages"]["h3"]
        self.registry.apply("h3", initial, 3, self.objects)
        self.assertEqual(self.registry.describe()["stages"]["h3"], initial)

    def test_replaced_bridge_node_keeps_current_job_constraints(self):
        def bridge_body(timeout, revision):
            return {"prompt": {
                "1": {"class_type": "AgentRuntimeBridgeText", "inputs": {
                    "prompt": "Ambient input", "cwd": timeout, "sandbox_mode": "read-only"}},
                "2": {"class_type": "PreviewAny", "inputs": {"source": ["1", 0]}},
            }, "extra_data": {"ambient": {"stage": "text", "sessionId": str(uuid4()),
                "revision": revision, "bindings": {"prompt": {"node": "1", "input": "prompt", "source": "ambient"}},
                "outputs": {"text": "2"}}}}
        self.registry.prepare(bridge_body("old-job", 0))
        template = self.registry.describe()["stages"]["text"]
        template["graph"]["10"] = template["graph"].pop("1")
        template["bindings"]["prompt"]["node"] = "10"
        template["graph"]["2"]["inputs"]["source"] = ["10", 0]
        objects = {
            "AgentRuntimeBridgeText": {"input": {"required": {
                "prompt": ["STRING"], "cwd": ["STRING"], "sandbox_mode": [["read-only"]]}}, "output": ["STRING"]},
            "PreviewAny": {"input": {"required": {"source": ["*"]}}, "output": []},
        }
        self.registry.apply("text", template, 0, objects)
        actual = self.registry.prepare(bridge_body("current-job", 1))
        self.assertEqual(actual["prompt"]["10"]["inputs"]["cwd"], "current-job")
        self.assertEqual(actual["prompt"]["10"]["inputs"]["sandbox_mode"], "read-only")

    def test_jev_schema_without_autogrow_connections_can_be_edited(self):
        body = recipe({"prompt": "Rainy forest"}, str(uuid4()), 0)
        self.registry.prepare(body)
        template = self.registry.describe()["stages"]["jev"]
        objects = {
            "JevInterpret": {"input": {"required": {
                "state": ["STRING"], "state_format": [["text", "json"]], "schema_json": ["STRING"],
                "refresh": ["INT"], "provider": [["typesafe", "openrouter"]], "api_key": ["STRING"],
                "model": [["jev-latest"]], "schemas": ["COMFY_AUTOGROW_V3", {"template": {
                    "prefix": "schema", "min": 0, "max": 100, "input": {"required": {"schema": ["JEV_SCHEMA"]}}}}]}}, "output": ["JEV_JUDGMENTS", "STRING"]},
            "JevResolve": {"input": {"required": {"judgments": ["JEV_JUDGMENTS"], "bindings_json": ["STRING"]}}, "output": ["JEV_RESULT", "DICT", "STRING"]},
            "PreviewAny": {"input": {"required": {"source": ["*"]}}, "output": []},
        }
        template["graph"]["1"]["inputs"]["provider"] = "openrouter"
        self.registry.apply("jev", template, 0, objects)
        actual = self.registry.prepare(recipe({"prompt": "New request"}, str(uuid4()), 1))
        self.assertEqual(actual["prompt"]["1"]["inputs"]["provider"], "openrouter")
        self.assertIn("New request", actual["prompt"]["1"]["inputs"]["state"])


if __name__ == "__main__":
    unittest.main()
