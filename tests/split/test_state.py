import tempfile
import unittest
from pathlib import Path

from comfy_split.state import Journal, job_history


class JournalTests(unittest.TestCase):
    def test_native_metadata_repairs_old_failures_without_changing_requests(self):
        body = {"prompt": {"1": {}}, "extra_data": {"extra_pnginfo": {"workflow": {"id": "wf"}}}}
        job = self.journal.enqueue(body, "retry")
        pending = self.journal.queue()["queue_pending"][0]
        self.assertEqual(pending[3]["create_time"], int(job["created_at"] * 1000))
        self.assertNotIn("create_time", body["extra_data"])
        job.update(status="failed", history={
            "prompt": [0, job["id"], body["prompt"], {}, []], "outputs": {},
            "status": {"status_str": "error", "completed": False, "messages": [
                ["execution_error", {"prompt_id": job["id"], "exception_message": "worker failed"}]]}})
        repaired = self.journal.history()[job["id"]]
        self.assertEqual(repaired["prompt"][3], pending[3])
        error = repaired["status"]["messages"][0][1]
        self.assertEqual(error["exception_message"], "worker failed")
        self.assertEqual(error["exception_type"], "RemoteExecutionError")
        self.assertEqual(error["traceback"], [])
        self.assertEqual(error["node_id"], "")
        self.assertEqual(error["node_type"], "")
        self.assertNotIn("exception_type", job["history"]["status"]["messages"][0][1])
        self.assertIs(self.journal.enqueue(body, "retry"), job)
        self.journal.save()
        self.assertEqual(Journal(Path(self.temp.name)).history()[job["id"]], repaired)

    def test_native_history_preserves_real_errors_outputs_and_timestamps(self):
        job = self.journal.enqueue({"prompt": {"1": {}}})
        job.update(status="failed", history={
            "prompt": [0, job["id"], {"1": {}}, {"create_time": 1234}, []],
            "outputs": {"1": {"images": [{"filename": "existing.png"}]}},
            "status": {"status_str": "error", "messages": [["execution_error", {
                "node_id": "1", "node_type": "SaveImage", "exception_type": "ValueError",
                "exception_message": "original", "traceback": ["original trace"]}]]}})
        result = job_history(job)
        self.assertEqual(result["prompt"][3]["create_time"], 1234)
        self.assertEqual(result["outputs"], job["history"]["outputs"])
        self.assertEqual(result["status"]["messages"][0][1]["traceback"], ["original trace"])


    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name))

    def test_idempotency_survives_restart_and_rejects_different_body(self):
        body = {"prompt": {"1": {"class_type": "Test"}}}
        first = self.journal.enqueue(body, "retry")
        self.journal.save()
        journal = Journal(Path(self.temp.name))
        self.assertEqual(journal.enqueue(body, "retry")["id"], first["id"])
        with self.assertRaises(ValueError):
            journal.enqueue({"prompt": {"different": {}}}, "retry")

    def test_unknown_dispatch_never_returns_to_queue(self):
        job = self.journal.enqueue({"prompt": {"1": {}}})
        job["status"] = "dispatching"
        self.journal.save()
        restarted = Journal(Path(self.temp.name))
        restarted.recover()
        self.assertEqual(restarted.data["jobs"][job["id"]]["status"], "unknown")
        self.assertIsNone(restarted.next_job())
        with self.assertRaises(ValueError):
            restarted.assert_idle()

    def test_running_call_is_recoverable_without_resubmission(self):
        job = self.journal.enqueue({"prompt": {"1": {}}})
        job.update(status="running", call_id="fc-existing")
        self.journal.save()
        restarted = Journal(Path(self.temp.name))
        restarted.recover()
        self.assertIsNone(restarted.next_job())
        self.assertEqual(restarted.data["jobs"][job["id"]]["call_id"], "fc-existing")

    def test_environment_and_mode_changes_reject_pending_work(self):
        self.journal.enqueue({"prompt": {"1": {}}})
        with self.assertRaises(ValueError):
            self.journal.assert_idle()
        self.journal.data["candidate"] = {"version": "env-new"}
        with self.assertRaises(ValueError):
            self.journal.enqueue({"prompt": {"2": {}}})
