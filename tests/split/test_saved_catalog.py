import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from comfy_split.saved_catalog import SavedCatalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data, self.models = self.root / "data", self.root / "models"
        self.library = self.data / "user/default/workflows"
        (self.library / "日本語").mkdir(parents=True)
        (self.models / "checkpoints/sub").mkdir(parents=True)
        (self.models / "checkpoints/sub/model.safetensors").write_bytes(b"weights")
        (self.models / "checkpoints/.secret").write_bytes(b"secret")
        (self.models / "checkpoints/link.ckpt").symlink_to(self.root / "private")
        (self.models / "linked").symlink_to(self.root, target_is_directory=True)
        self.api = {"1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64}}}
        (self.library / "日本語/API.json").write_text(json.dumps(self.api))
        (self.library / "編集.json").write_text(json.dumps({"nodes": [{"type": "CustomNode"}], "links": []}))
        self.catalog = SavedCatalog(self.data, self.models)
        (self.data / "state").mkdir()
        (self.data / "jobs").mkdir()
        self.id = str(uuid.uuid4())
        self.job = dict(id=self.id, status="running", created_at=100,
                        body={"prompt": self.api, "extra_data": {"api_key": "private"}})
        self.state = dict(jobs={self.id: self.job}, retired_jobs={})
        self.save_state()

    def save_state(self):
        (self.data / "state/controller.json").write_text(json.dumps(self.state))

    def result(self, **changes):
        receipt = dict(status="completed", finished_at=103, seconds=2, history={
            "outputs": {"2": {"images": [{"filename": "x.png", "type": "output"}]}},
            "status": {"status_str": "success", "completed": True, "messages": [
                ["execution_start", {"timestamp": 100000}],
                ["execution_success", {"timestamp": 102000}]]}})
        receipt.update(changes)
        (self.data / f"jobs/{self.id}.json").write_text(json.dumps(receipt))

    def test_models_are_storage_metadata_not_runtime_recognition(self):
        result = self.catalog.model_list({})
        self.assertEqual(result["categories"], ["checkpoints"])
        self.assertEqual(result["recognition"], "not_checked")
        result = self.catalog.model_list({"category": "checkpoints", "query": "MODEL", "limit": "1"})
        self.assertEqual(result["models"], ["sub/model.safetensors"])
        self.assertEqual(result["entries"][0]["size_bytes"], 7)
        self.assertIsInstance(result["entries"][0]["modified_at"], float)
        self.assertEqual(self.catalog.model_list({"category": "checkpoints", "offset": "1"})["models"], [])
        for query in [{"category": "../private"}, {"limit": "0"}, {"source": "runtime"}, {"category": "x/y"}]:
            with self.assertRaises(ValueError):
                self.catalog.model_list(query)

    def test_workflow_raw_and_api_graph_read_without_node_definitions(self):
        self.assertEqual(self.catalog.workflow({"action": "list"})["workflows"], ["日本語/API.json", "編集.json"])
        r = self.catalog.workflow({"action": "get", "filename": "日本語/API.json"})
        self.assertFalse(r["conversion_required"])
        self.assertEqual(r["workflow"], self.api)
        self.assertTrue(self.catalog.workflow({"action": "get", "filename": "編集.json"})["conversion_required"])
        r = self.catalog.workflow({"action": "get", "filename": "編集.json", "format": "ui"})
        self.assertFalse(r["conversion_required"])
        self.assertEqual(r["workflow"]["nodes"][0]["type"], "CustomNode")

    def test_read_roots_and_symlinks_cannot_escape_and_truncation_is_not_empty(self):
        (self.library / "leak.json").symlink_to(self.data / "state/controller.json")
        (self.library / "alias").symlink_to(self.data / "state", target_is_directory=True)
        os.mkfifo(self.library / "pipe.json")
        self.assertEqual(len(self.catalog.workflow({"action": "list"})["workflows"]), 2)
        for name in ["../state/controller.json", "leak.json", "alias/controller.json", "pipe.json", "/x.json"]:
            with self.assertRaises((OSError, ValueError)):
                self.catalog.workflow({"action": "get", "filename": name, "format": "ui"})
        with patch("comfy_split.saved_catalog.MAX_ENTRIES", 1):
            with self.assertRaises(ValueError):
                self.catalog.workflow({"action": "list"})
        with self.assertRaises(ValueError):
            self.catalog.workflow({"action": "delete", "filename": "編集.json"})

    def test_receipt_overrides_stale_running_controller_without_leaking_extra_data(self):
        self.result()
        r = self.catalog.history({"action": "list"})
        self.assertEqual(r["items"][0]["status"], "completed")
        self.assertEqual(r["items"][0]["execution_seconds"], 2)
        self.assertEqual(r["items"][0]["elapsed_seconds"], 3)
        self.assertNotIn("private", json.dumps(r))
        r = self.catalog.job_workflow({"prompt_id": self.id})
        self.assertEqual(r["workflow"], self.api)
        self.assertEqual(r["status"], "completed")
        self.assertNotIn("private", json.dumps(r))

    def test_diagnosis_failed_cancelled_unknown_retired_and_active_snapshot(self):
        self.result(status="failed", error="Test failure", history=None)
        r = self.catalog.history({"action": "diagnose"})
        self.assertEqual(r["items"][0]["error"]["exception_message"], "Test failure")
        self.result(status="cancelled", history=None)
        self.assertEqual(self.catalog.history({"action": "list"})["items"][0]["status"], "cancelled")
        with self.assertRaises(FileNotFoundError):
            self.catalog.history({"action": "list", "prompt_id": str(uuid.uuid4())})
        self.state["retired_jobs"][self.id] = {"status": "completed"}
        self.save_state()
        with self.assertRaises(FileNotFoundError):
            self.catalog.job_workflow({"prompt_id": self.id})
        self.state["retired_jobs"] = {}
        self.save_state()
        (self.data / f"jobs/{self.id}.json").unlink()
        r = self.catalog.history({"action": "list", "prompt_id": self.id})
        self.assertFalse(r["live"])
        self.assertIsNone(r["items"][0]["elapsed_seconds"])
        self.assertEqual(self.catalog.history({"action": "list"})["items"], [])


if __name__ == "__main__":
    unittest.main()
