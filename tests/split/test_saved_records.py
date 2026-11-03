"""Saved reads stay independent of controller size, receipts and review payloads."""

import json
import tempfile
import unittest
from pathlib import Path

from comfy_split.media_server import saved_job
from comfy_split.saved_catalog import SavedCatalog
from comfy_split.state import Journal, write_json


class SavedRecordsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.journal = Journal(self.root / "state")
        self.catalog = SavedCatalog(self.root, self.root / "models")
        self.graph = {"1": {"class_type": "EmptyImage", "inputs": {}}}

    def test_large_controller_does_not_break_receiptless_history_or_saved_graph(self):
        job = self.journal.enqueue({"prompt": self.graph, "extra_data": {"key": "private-key"}})
        job.update(status="failed", error="controller-side failure", finished_at=123)
        self.journal.data["retired_jobs"] = {"historical-padding": "x" * (33 * 1024 * 1024)}
        self.journal.save()
        self.assertGreater(self.journal.path.stat().st_size, 32 * 1024 * 1024)
        self.assertEqual(self.catalog.job_workflow({"prompt_id": job["id"]})["workflow"], self.graph)
        items = self.catalog.history({"action": "list"})["items"]
        self.assertEqual(items[0]["error"]["exception_message"], "controller-side failure")
        saved = saved_job(self.root, job["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertNotIn("private-key", json.dumps(saved))
        self.assertFalse((self.root / "jobs").exists())

    def test_receipt_merges_with_saved_graph_but_cannot_resurrect_retired_job(self):
        job = self.journal.enqueue({"prompt": self.graph})
        job["status"] = "running"
        self.journal.save()
        write_json(self.root / "jobs" / (job["id"] + ".json"),
                   {"status": "completed", "history": {"outputs": {"2": {"images": []}}}})
        self.assertEqual(saved_job(self.root, job["id"])["status"], "completed")
        self.assertEqual(len(self.catalog.history({"action": "list"})["items"]), 1)
        # Restart before retiring so cleanup cannot rely on an in-memory cache.
        restarted = Journal(self.root / "state")
        restarted.data["jobs"][job["id"]]["status"] = "completed"
        restarted.retire([job["id"]])
        restarted.save()
        self.assertFalse((self.root / "state/saved-jobs" / (job["id"] + ".json")).exists())
        self.assertIsNone(saved_job(self.root, job["id"]))
        self.assertEqual(self.catalog.history({"action": "list"})["items"], [])

    def test_pending_bodies_are_separate_hydrated_and_deleted_after_decision(self):
        rows = {}
        for i in range(64):
            key = f"{i:08x}.00000000.00000000.00000000"
            rows[key] = {"id": key, "status": "pending", "body": {"text": "x" * 600000},
                         "calls": [], "assets": [], "csrf": "fixture"}
        self.journal.data["mcp_reviews"] = rows
        self.journal.save()
        self.assertLess(self.journal.path.stat().st_size, 32768)
        restarted = Journal(self.root / "state")
        self.assertEqual(restarted.data["mcp_reviews"], rows)
        key = next(iter(rows))
        payload = self.root / "state/review-payloads" / (key + ".json")
        from comfy_split.mcp_approvals import close
        close(restarted.data["mcp_reviews"][key], "denied")
        restarted.save()
        self.assertFalse(payload.exists())
        self.assertEqual(Journal(self.root / "state").data["mcp_reviews"][key]["status"], "denied")
        key = list(rows)[1]
        (self.root / "state/review-payloads" / (key + ".json")).unlink()
        restarted = Journal(self.root / "state")
        self.assertEqual(restarted.data["mcp_reviews"][key]["status"], "invalidated")
        self.assertNotIn("csrf", restarted.data["mcp_reviews"][key])

