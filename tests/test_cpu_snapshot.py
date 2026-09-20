import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from comfy_split import cpu_snapshot, storage
from comfy_split.gateway import Controller
from comfy_split.state import Journal, write_json


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.volumes = {key: SimpleNamespace(reload=SimpleNamespace(aio=AsyncMock()),
            commit=SimpleNamespace(aio=AsyncMock())) for key in ("data", "environment")}

    async def test_resume_forwards_current_credentials_in_private_one_use_file(self):
        path = self.root / "resume.json"
        helper = SimpleNamespace(poll=lambda: None)
        with patch.object(cpu_snapshot, "RESUME", path), \
             patch.object(cpu_snapshot, "_helper", helper), \
             patch.dict(os.environ, {"MODAL_TASK_ID": "new-container", "MODAL_TOKEN_SECRET": "test-only"}):
            self.assertIs(cpu_snapshot.resume(), helper)
        payload = json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(payload["container_id"], "new-container")
        self.assertEqual(payload["environment_variables"]["MODAL_TOKEN_SECRET"], "test-only")
        self.assertTrue(payload["restoration_id"])

    async def test_restore_reads_new_state_and_never_dispatches_captured_jobs(self):
        state = self.root / "state"
        journal = Journal(state)
        old = journal.enqueue({"prompt": {"1": {}}}, "existing")
        old.update(status="running", call_id="fc-existing")
        journal.save()
        # The mount refresh publishes a newer journal before the constructor.
        async def reload():
            old.update(status="completed")
            journal.data["environment"] = "env-new"
            journal.data["ambient_workflows"] = {"revision": 9}
            journal.save()
        self.volumes["data"].reload.aio.side_effect = reload
        cpu = SimpleNamespace(version=None, user_directory="captured", temp_namespace="captured")
        worker = Mock()
        def make(volumes, warmed_cpu):
            return Controller(worker, None, None, volumes, state, warmed_cpu=warmed_cpu)
        with patch("comfy_split.gateway.make_controller", side_effect=make), \
             patch.object(cpu_snapshot, "USER_LINK", self.root / "user-link"), \
             patch.object(storage, "USER", self.root / "live-user"):
            controller = await cpu_snapshot.restore_controller(cpu,
                {"environment": "env-old", "initialization_id": "captured"},
                volumes=self.volumes, restoration={"restoration_id": "fresh"})
        self.assertEqual(controller.journal.data["environment"], "env-new")
        self.assertEqual(controller.journal.data["jobs"][old["id"]]["status"], "completed")
        self.assertEqual(controller.journal.data["ambient_workflows"], {"revision": 9})
        self.assertIsNone(controller.task)
        self.assertEqual(controller.sockets, {})
        self.assertIs(controller.cpu, cpu)
        self.assertIsNone(cpu.user_directory)
        self.assertNotEqual(cpu.temp_namespace, "captured")
        self.assertEqual(controller.snapshot_status["restoration_id"], "fresh")
        worker.assert_not_called()
        self.volumes["data"].commit.aio.assert_not_awaited()

    async def test_new_environment_replaces_warm_runtime_before_dispatch(self):
        controller = Controller(Mock(), None, None, self.volumes, self.root)
        controller.journal.data["environment"] = "env-new"
        controller.snapshot_status = {"environment": "env-old"}
        controller.cpu.version = "env-old"
        controller.cpu.process = SimpleNamespace(returncode=None)
        order = []
        async def start(version, **kwargs):
            order.append(("start", version))
            controller.cpu.version = version
            controller.cpu.process = SimpleNamespace(returncode=None)
        async def dispatch():
            order.append(("dispatch", controller.cpu.version))
        controller.cpu.start = AsyncMock(side_effect=start)
        controller.cpu.stop = AsyncMock()
        controller.refresh_ambient_nodes = AsyncMock()
        controller.dispatch = AsyncMock(side_effect=dispatch)
        with patch("comfy_split.gateway.initialize_environment"):
            await controller.start(None)
        await controller.task
        self.assertEqual(order, [("start", "env-new"), ("dispatch", "env-new")])
        self.assertFalse(controller.snapshot_status["reused"])
        await controller.close(None)

    async def test_snapshot_pin_survives_job_retention(self):
        data = {"environment": "env-new", "candidate": None, "session": None, "jobs": {}}
        plan = storage.cleanup_plan(data, ["env-old", "env-new", "env-unused"], [], [],
                                    snapshot_environments={"env-old"})
        self.assertEqual(plan["environments"], ["env-unused"])
        self.assertEqual(plan["protected_environments"]["env-old"], "CPU memory snapshot")

    async def test_loaded_environment_reload_waits_until_process_stops(self):
        cpu = SimpleNamespace(version="env-old", url="http://test", stop=AsyncMock())
        response = SimpleNamespace(raise_for_status=Mock(), json=AsyncMock(return_value={"cpu_guard": True}))
        context = AsyncMock()
        context.__aenter__.return_value = response
        client = SimpleNamespace(post=Mock(return_value=context))
        session = AsyncMock()
        session.__aenter__.return_value = client
        controller = SimpleNamespace(journal=SimpleNamespace(data={"mode": "split", "environment": "env-old"}))
        async def safe_reload():
            self.assertTrue(cpu.stop.await_count, "loaded .so mappings must be closed first")
        self.volumes["environment"].reload.aio.side_effect = safe_reload
        with patch("aiohttp.ClientSession", return_value=session), \
             patch("comfy_split.gateway.make_controller", return_value=controller), \
             patch.object(cpu_snapshot, "USER_LINK", self.root / "user-link"), \
             patch.object(storage, "USER", self.root / "live-user"):
            await cpu_snapshot.restore_controller(cpu, {}, volumes=self.volumes, restoration={})
            self.volumes["data"].reload.aio.assert_awaited_once()
            self.volumes["environment"].reload.aio.assert_not_awaited()
            cpu.stop.assert_not_awaited()
            controller.journal.data["environment"] = "env-new"
            await cpu_snapshot.restore_controller(cpu, {}, volumes=self.volumes, restoration={})
            cpu.stop.assert_awaited_once()
            self.volumes["environment"].reload.aio.assert_awaited_once()
            # A Manager draft can be newer even while the active revision is
            # unchanged. Reload it only after releasing the warm process too.
            cpu.stop.reset_mock()
            self.volumes["environment"].reload.aio.reset_mock()
            controller.journal.data.update(environment="env-old", candidate={"version": "env-draft"})
            await cpu_snapshot.restore_controller(cpu, {}, volumes=self.volumes, restoration={})
            cpu.stop.assert_awaited_once()
            self.volumes["environment"].reload.aio.assert_awaited_once()

    async def test_pins_are_read_remotely_and_bad_pins_prevent_cleanup(self):
        async def entries(*args, **kwargs):
            yield SimpleNamespace(path="/.cpu-snapshots/deployment.json")
        async def read(*args):
            yield json.dumps({"environment": "env-old"}).encode()
        volume = SimpleNamespace(iterdir=SimpleNamespace(aio=entries),
                                  read_file=SimpleNamespace(aio=read))
        self.assertEqual(await storage.snapshot_environments(volume), {"env-old"})
        async def broken(*args):
            yield b"{}"
        volume.read_file.aio = broken
        with self.assertRaises(KeyError):
            await storage.snapshot_environments(volume)

    async def test_warmup_selects_only_existing_split_environment(self):
        with patch.object(storage, "STATE", self.root / "state"), \
             patch("comfy_split.cpu_snapshot.environment_path", return_value=self.root / "env"):
            self.assertIsNone(cpu_snapshot.selected_environment())
            write_json(storage.STATE / "controller.json", {"mode": "split", "environment": "env-old"})
            self.assertIsNone(cpu_snapshot.selected_environment())
            write_json(self.root / "env/ready.json", {})
            self.assertEqual(cpu_snapshot.selected_environment(), "env-old")
            write_json(storage.STATE / "controller.json", {"mode": "legacy", "environment": "env-old"})
            self.assertIsNone(cpu_snapshot.selected_environment())
