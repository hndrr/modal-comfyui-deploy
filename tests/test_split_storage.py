import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from comfy_split import runtime, storage, worker
from comfy_split.gateway import Controller
from comfy_split.state import Journal


class Volume:
    """Filesystem-backed control-plane API; tracks commit/delete ordering."""
    def __init__(self, root, events):
        self.root, self.events = root, events
        self.root.mkdir(parents=True, exist_ok=True)
        self.commit = SimpleNamespace(aio=AsyncMock(side_effect=lambda: events.append("commit")))
        self.reload = SimpleNamespace(aio=AsyncMock())
        self.read_file = SimpleNamespace(aio=self.read)
        self.iterdir = SimpleNamespace(aio=self.list)
        self.remove_file = SimpleNamespace(aio=self.remove)

    async def read(self, name):
        yield (self.root / name.lstrip("/")).read_bytes()

    async def list(self, name, recursive=True):
        root = self.root / name.lstrip("/")
        if not root.exists():
            raise FileNotFoundError(name)
        for path in root.rglob("*") if recursive else root.iterdir():
            yield SimpleNamespace(path=path.relative_to(self.root).as_posix(),
                                  mtime=path.stat().st_mtime, size=path.stat().st_size,
                                  type=3 if path.is_symlink() else 2 if path.is_dir() else 1)

    async def remove(self, name, recursive=False):
        self.events.append("delete:" + name)
        path = self.root / name.lstrip("/")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000
        self.old = self.now - storage.HISTORY_SECONDS
        self.data = {"environment": "env-active", "candidate": {"version": "env-candidate"},
                     "session": None, "jobs": {
                         "old": {"status": "completed", "finished_at": self.old},
                         "recent": {"status": "completed", "finished_at": self.old + 1},
                         "undated": {"status": "cancelled"}}}

    def plan(self, receipts=(), temporary=()):
        return storage.cleanup_plan(self.data, ["base", "env-active", "env-candidate", "env-old", "unowned"],
                                    receipts, temporary, now=self.now)

    def test_completion_age_and_environment_ownership(self):
        plan = self.plan()
        self.assertEqual(plan["expired_jobs"], ["old"])
        self.assertEqual(plan["environments"], ["env-old"])
        for status in ("queued", "dispatching", "running", "unknown"):
            self.data["jobs"]["old"].update(status=status, environment="env-old")
            self.assertEqual(self.plan()["expired_jobs"], [])
            self.assertEqual(self.plan()["environments"], [])

    def test_history_references_and_temporary_file_age(self):
        self.data["jobs"]["recent"]["history"] = {"outputs": {"1": {"images": [
            {"filename": "keep.png", "subfolder": ".split-temp/session", "type": "output"}]}}}
        temporary = [{"path": path, "mtime": mtime} for path, mtime in (
            ("session/keep.png", 0), ("session/delete.png", self.now - storage.TEMP_SECONDS),
            ("session/new.png", self.now - storage.TEMP_SECONDS + 1), ("../outside.png", 0))]
        self.assertEqual(self.plan(temporary=temporary)["temporary_files"], ["session/delete.png"])
        self.data["jobs"]["old"]["status"] = "unknown"
        self.assertEqual(self.plan(temporary=temporary)["temporary_files"], [])

    def test_unknown_receipts_are_preserved_even_without_a_journal_owner(self):
        receipts = [{"path": "orphan.started.json", "mtime": self.old, "result": {}}]
        plan = self.plan(receipts, [{"path": "temp/a.png", "mtime": 0}])
        self.assertTrue(plan["unresolved_receipts"])
        self.assertEqual(plan["receipts"], [])
        self.assertEqual(plan["temporary_files"], [])
        self.assertEqual(plan["environments"], [])

    def test_expired_terminal_receipts_and_unexpired_result_references(self):
        receipts = [
            {"path": "old.started.json", "mtime": self.old, "result": {}},
            {"path": "old.json", "mtime": self.old, "result": {"status": "completed"}},
            {"path": "recent.json", "mtime": self.old, "result": {"status": "completed"}},
        ]
        self.assertEqual(self.plan(receipts)["receipts"], ["old.json", "old.started.json"])

    def test_copying_receipts_does_not_extend_completed_job_retention(self):
        receipts = [{"path": "old.started.json", "mtime": self.now, "result": {}},
                    {"path": "old.json", "mtime": self.now, "result": {"status": "completed"}}]
        self.assertEqual(self.plan(receipts)["receipts"], ["old.json", "old.started.json"])


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []
        self.volumes = {key: Volume(self.root / key, self.events) for key in ("data", "environment", "output")}
        self.stats = SimpleNamespace(num_total_runners=0, backlog=0)
        self.worker = SimpleNamespace(get_current_stats=SimpleNamespace(aio=AsyncMock(return_value=self.stats)))
        self.control = Controller(self.worker, None, None, self.volumes, self.root / "data/state")
        self.control.cpu = SimpleNamespace(process=None)
        self.control.journal.data["environment"] = "env-active"
        for name in ("base/comfy/custom_nodes/node", "env-active", "env-old"):
            (self.root / "environment" / name).mkdir(parents=True)
        (self.root / "environment/base/comfy/main.py").write_text("unused core copy")
        self.job = self.control.journal.enqueue({"prompt": {"1": {}}}, "same-key")
        self.job.update(status="completed", finished_at=time.time() - storage.HISTORY_SECONDS - 10)
        self.job_id = self.job["id"]
        for suffix, content in ((".json", {"status": "completed"}), (".started.json", {})):
            path = self.root / "data/jobs" / (self.job_id + suffix)
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(content))
            os.utime(path, (0, 0))
        archive = self.root / "output/.split-temp/old"
        archive.mkdir(parents=True)
        (archive / "preview.png").write_bytes(b"preview")
        os.utime(archive / "preview.png", (0, 0))
        (self.root / "output/final.mp4").write_bytes(b"final")

    async def test_cleanup_preserves_outputs_and_replay_guards_across_restart(self):
        await self.control.cleanup_storage()
        journal = Journal(self.root / "data/state")
        self.assertNotIn(self.job_id, journal.data["jobs"])
        self.assertEqual(journal.enqueue({"prompt": {"1": {}}}, "same-key")["id"], self.job_id)
        with self.assertRaises(ValueError):
            journal.enqueue({"prompt": {"2": {}}}, "same-key")
        self.assertTrue((self.root / "output/final.mp4").exists())
        self.assertFalse((self.root / "output/.split-temp/old/preview.png").exists())
        self.assertFalse((self.root / "output/.split-temp/old").exists())
        self.assertFalse((self.root / "environment/env-old").exists())
        self.assertFalse((self.root / "environment/base/comfy/main.py").exists())
        self.assertTrue((self.root / "environment/base/comfy/custom_nodes/node").exists())
        self.assertEqual(self.events[0], "commit")
        # A replay arriving after receipt cleanup still cannot launch ComfyUI.
        process = SimpleNamespace(version=None, start=AsyncMock(), stop=AsyncMock())
        volumes = dict(self.volumes)
        for key in ("input", "models"):
            volumes[key] = Volume(self.root / key, [])
        with patch.object(worker, "STATE", self.root / "data/state"), \
             patch.object(worker, "JOBS", self.root / "data/jobs"), patch.object(worker, "process", process):
            result = await worker.run_worker({"id": self.job_id, "environment": "env-active", "operation": "generate"},
                None, None, volumes)
        self.assertTrue(result["history_expired"])
        process.start.assert_not_awaited()

    async def test_commit_failure_keeps_all_recovery_files(self):
        self.volumes["data"].commit.aio.side_effect = OSError("commit failed")
        with self.assertRaises(OSError):
            await self.control.cleanup_storage()
        self.assertFalse(any(event.startswith("delete:") for event in self.events))
        self.assertTrue((self.root / "data/jobs" / (self.job_id + ".started.json")).exists())

    async def test_gpu_containers_backlog_and_unknown_work_prevent_deletion(self):
        for runners, backlog in ((1, 0), (0, 1)):
            self.stats.num_total_runners, self.stats.backlog = runners, backlog
            self.control.cleanup_due = 0
            await self.control.cleanup_storage()
            self.assertEqual(self.events, [])
        self.stats.num_total_runners = self.stats.backlog = 0
        self.job.update(status="unknown", environment="env-old")
        self.control.cleanup_due = 0
        await self.control.cleanup_storage()
        self.assertTrue((self.root / "environment/env-old").exists())
        self.assertTrue((self.root / "data/jobs" / (self.job_id + ".json")).exists())

    async def test_discarded_and_superseded_environments_do_not_accumulate(self):
        self.control.journal.data["candidate"] = {"version": "env-old", "status": "editing"}
        await self.control.cleanup_storage()
        self.assertTrue((self.root / "environment/env-old").exists())
        self.control.journal.data["candidate"] = None
        for number in range(3):
            self.control.cleanup_due = 0
            new = "env-new" + str(number)
            (self.root / "environment" / new).mkdir()
            self.control.journal.data["environment"] = new
            await self.control.cleanup_storage()
            self.assertEqual({p.name for p in (self.root / "environment").iterdir()}, {"base", new})

    async def test_result_reads_use_file_api_without_reloading_user_data(self):
        result = await self.control.read_result(self.job)
        self.assertEqual(result["status"], "completed")
        self.volumes["data"].reload.aio.assert_not_awaited()


class InitializationTests(unittest.TestCase):
    def test_new_base_copies_nodes_without_copying_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template"
            (template / "custom_nodes/pack").mkdir(parents=True)
            (template / "custom_nodes/pack/__init__.py").write_text("node")
            (template / "main.py").write_text("core")
            with patch.object(runtime, "TEMPLATE", template), patch.object(runtime, "ENVIRONMENTS", root / "envs"), \
                 patch.object(runtime, "USER", root / "user"), patch.object(runtime.subprocess, "run", Mock()):
                runtime.initialize_environment()
            self.assertFalse((root / "envs/base/comfy/main.py").exists())
            self.assertTrue((root / "envs/base/comfy/custom_nodes/pack/__init__.py").exists())


if __name__ == "__main__":
    unittest.main()
