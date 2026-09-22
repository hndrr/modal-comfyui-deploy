"""Fix the order of competing writes at the remote-store boundary, without a GPU."""

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from ambient.api import create_api
from ambient.job_state import finish_job
from ambient.processing import run_job
from ambient.service import JobService
from test_ambient import Volume, request


class Store:
    def __init__(self):
        self.records = {}
        self.before_terminal_write = None

    def get(self, key, default=None):
        return copy.deepcopy(self.records.get(key, default))

    def __getitem__(self, key):
        return copy.deepcopy(self.records[key])

    def put(self, key, value, skip_if_exists=False):
        if key.startswith("terminal:") and self.before_terminal_write:
            callback, self.before_terminal_write = self.before_terminal_write, None
            callback(value)
        if skip_if_exists and key in self.records:
            return False
        self.records[key] = copy.deepcopy(value)
        return True


class FinalizationTest(unittest.TestCase):
    def setUp(self):
        self.jobs = Store()
        self.service = JobService(self.jobs, lambda _: "fc-local", now=lambda: 20)
        self.inputs, self.outputs = Volume(), Volume()
        self.req = request()
        self.job_id = self.req["requestId"]
        self.service.submit(self.req)
        self.client = self.enterContext(TestClient(create_api(
            self.service, lambda: {}, self.inputs, self.outputs,
        )))

        def publish(source, job_id):
            self.outputs.files[f"ambient/clips/{job_id}.mp4"] = source.read_bytes()
            return {"id": job_id, "hasAudio": True}

        self.storage = SimpleNamespace(
            prepare_anchor=lambda *args: None, publish_clip=publish,
        )

    def run_worker(self, generator=None):
        def generate(req, image, source, cancelled, progress):
            source.write_bytes(b"video")

        run_job(self.job_id, self.jobs, self.storage, {
            ("h3", "comfyui"): generator or generate,
        })

    def assert_stable(self, status):
        result = self.service.get(self.job_id)
        self.assertEqual(result["status"], status)
        self.assertEqual(self.client.get("/jobs/" + self.job_id).json(), result)
        for _ in range(2):
            self.assertEqual(self.client.delete("/jobs/" + self.job_id).json(), result)
        # A fresh service and an identical submission must restore the same result.
        spawn = Mock()
        restarted = JobService(self.jobs, spawn)
        self.assertEqual(restarted.get(self.job_id), result)
        self.assertEqual(restarted.submit(self.req), result)
        spawn.assert_not_called()
        download = self.client.get("/clips/" + self.job_id)
        self.assertEqual(download.status_code, 200 if status == "completed" else 404)
        if status == "completed":
            self.assertEqual(download.content, b"video")
            self.assertEqual(result["clip"]["id"], self.job_id)
        else:
            self.assertNotIn("clip", result)
        return result

    def test_cancel_wins_after_worker_last_check_before_completion_claim(self):
        def cancel_before_write(candidate):
            self.assertEqual(candidate["status"], "completed")
            self.assertIn(f"ambient/clips/{self.job_id}.mp4", self.outputs.files)
            self.assertEqual(self.service.cancel(self.job_id)["status"], "cancelled")

        self.jobs.before_terminal_write = cancel_before_write
        self.run_worker()
        self.assert_stable("cancelled")

    def test_completion_wins_after_cancel_read_before_cancel_claim(self):
        def complete_before_write(candidate):
            self.assertEqual(candidate["status"], "cancelled")
            self.run_worker()
            self.assertEqual(self.service.get(self.job_id)["status"], "completed")

        self.jobs.before_terminal_write = complete_before_write
        response = self.client.delete("/jobs/" + self.job_id)
        self.assertEqual(response.json()["status"], "completed")
        self.assert_stable("completed")

    def test_late_progress_and_failure_cannot_undo_completion(self):
        self.run_worker()
        self.assert_stable("completed")
        job = self.jobs[self.job_id]
        job.update(status="running", stage="Late progress")
        self.jobs.put(self.job_id, job)
        finish_job(self.jobs, self.job_id, status="failed", stage="Failed", error="late")
        self.assertNotIn("error", self.assert_stable("completed"))

    def test_worker_failure_after_accepted_cancel_stays_cancelled(self):
        def fail(*args):
            self.assertEqual(self.service.cancel(self.job_id)["status"], "cancelled")
            raise RuntimeError("transport failed while cancelling")

        with patch("builtins.print"):
            self.run_worker(fail)
        self.assertNotIn("error", self.assert_stable("cancelled"))

    def test_failure_wins_after_cancel_read_before_cancel_claim(self):
        def fail_before_write(candidate):
            self.assertEqual(candidate["status"], "cancelled")
            with patch("builtins.print"):
                self.run_worker(Mock(side_effect=RuntimeError("generation failed")))
            self.assertEqual(self.service.get(self.job_id)["status"], "failed")

        self.jobs.before_terminal_write = fail_before_write
        response = self.client.delete("/jobs/" + self.job_id)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(self.assert_stable("failed")["error"], "generation failed")

    def test_completion_during_reconciliation_keeps_its_result(self):
        job = self.jobs[self.job_id]
        job["createdAt"] = 0
        self.jobs.put(self.job_id, job)

        def reconcile(call_id):
            self.run_worker()
            return "Worker exited"

        self.service.reconcile = reconcile
        self.assert_stable("completed")

    def test_reconciled_failure_cannot_be_replaced_by_late_completion(self):
        job = self.jobs[self.job_id]
        job["createdAt"] = 0
        self.jobs.put(self.job_id, job)
        self.service.reconcile = lambda _: "Worker timed out"
        self.assert_stable("failed")
        self.run_worker()
        self.assertEqual(self.assert_stable("failed")["error"], "Worker timed out")

    def test_legacy_cancellation_survives_a_late_worker(self):
        self.jobs.put("cancel:" + self.job_id, True)
        generator = Mock()
        self.run_worker(generator)
        generator.assert_not_called()
        finish_job(self.jobs, self.job_id, status="completed", stage="Complete", clip={})
        self.assert_stable("cancelled")

    def test_legacy_completed_result_is_preserved_by_terminal_claim(self):
        job = self.jobs[self.job_id]
        job.update(status="completed", stage="Complete", clip={"id": self.job_id})
        self.jobs.put(self.job_id, job)
        self.outputs.files[f"ambient/clips/{self.job_id}.mp4"] = b"video"
        finish_job(self.jobs, self.job_id, status="cancelled", stage="Cancelled")
        self.assert_stable("completed")
