import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from comfy_split import node_packs
from comfy_split.config import NODE_PACKS, Settings
from comfy_split.gateway import Controller
from comfy_split.state import write_json
from tests.split.support import volume_mocks

MANAGED_SETTINGS = Settings(node_packs=tuple(NODE_PACKS))


class ManagedStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.volumes = volume_mocks("environment", "data")
        self.worker = Mock()
        self.control = Controller(self.worker, None, None, self.volumes, Path(self.directory.name), extensions=())
        self.catalog = {"objects": {name: {"python_module": "custom_nodes." + name}
                                    for name in node_packs.NODE_NAMES}}
        self.control.candidate = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(),
                                                catalog=AsyncMock(return_value=self.catalog))
        self.enterContext(patch.dict(os.environ, {**MANAGED_SETTINGS.environment(),
                              node_packs.DEPLOYMENT_ENV: "deployment-1"}))
        self.prepare = self.enterContext(patch.object(node_packs, "prepare_environment", return_value="env-new"))
        self.revisions = {name: "a" * 40 for name in node_packs.NODE_NAMES}
        self.snapshot = self.enterContext(patch.object(node_packs, "snapshot_revisions",
                                     return_value=self.revisions))

    async def test_refresh_commits_snapshot_before_selecting_it_and_never_wakes_gpu(self):
        selected_during_commit = []
        async def commit():
            selected_during_commit.append(self.control.journal.data["environment"])
        self.volumes["environment"].commit.aio.side_effect = commit
        await self.control.refresh_node_packs()
        self.assertEqual(selected_during_commit, ["base"])
        self.assertEqual(self.control.journal.data["environment"], "env-new")
        self.assertEqual(json.loads(self.control.journal.path.read_text())["environment"], "env-new")
        self.control.candidate.start.assert_awaited_once_with("env-new", cpu=True)
        self.assertEqual(self.worker.mock_calls, [])

    async def test_disabled_busy_and_editing_startups_do_not_fetch(self):
        with patch.dict(os.environ, Settings().environment()):
            await self.control.refresh_node_packs()
        for status in ("queued", "running", "unknown"):
            self.control.journal.data["jobs"] = {"job": {"status": status}}
            await self.control.refresh_node_packs()
        self.control.journal.data["jobs"] = {}
        self.control.journal.data["candidate"] = {"status": "editing"}
        await self.control.refresh_node_packs()
        self.prepare.assert_not_called()

    async def test_fetch_dependency_and_import_failures_keep_previous_environment(self):
        for error in (RuntimeError("private repository unavailable"),
                      RuntimeError("dependency conflict"), None):
            self.control.journal.data.pop(node_packs.REFRESH_KEY, None)
            self.prepare.side_effect = error
            self.control.candidate.catalog.return_value = {"objects": {}}
            with self.assertLogs("comfy_split.gateway", level="ERROR"):
                await self.control.refresh_node_packs()
            self.assertEqual(self.control.journal.data["environment"], "base")
        self.volumes["environment"].commit.aio.assert_not_awaited()
        self.assertEqual(self.worker.mock_calls, [])

    async def test_cold_restart_reuses_persisted_snapshot_without_github_access(self):
        self.prepare.return_value = None
        await self.control.refresh_node_packs()
        restarted = Controller(self.worker, None, None, self.volumes, Path(self.directory.name), extensions=())
        self.prepare.reset_mock()
        self.prepare.side_effect = AssertionError("ordinary startup must not use GitHub")
        with patch.dict(os.environ, {node_packs.TOKEN_ENV: ""}):
            await restarted.refresh_node_packs()
        self.prepare.assert_not_called()
        self.assertEqual(self.worker.mock_calls, [])

    async def test_new_deployment_rechecks_but_manager_copy_reuses_saved_nodes(self):
        self.prepare.return_value = None
        await self.control.refresh_node_packs()
        self.prepare.reset_mock()
        self.control.journal.data["environment"] = "env-manager-copy"
        await self.control.refresh_node_packs()
        self.prepare.assert_not_called()
        with patch.dict(os.environ, {node_packs.DEPLOYMENT_ENV: "deployment-2"}):
            await self.control.refresh_node_packs()
        self.prepare.assert_called_once_with("env-manager-copy")

    async def test_failed_update_with_valid_snapshot_is_not_retried_on_each_cold_start(self):
        self.prepare.side_effect = RuntimeError("GitHub unavailable")
        with self.assertLogs("comfy_split.gateway", level="ERROR"):
            await self.control.refresh_node_packs()
        self.assertEqual(self.control.journal.data[node_packs.REFRESH_KEY]["status"], "failed")
        restarted = Controller(self.worker, None, None, self.volumes, Path(self.directory.name), extensions=())
        await restarted.refresh_node_packs()
        self.prepare.assert_called_once()
        self.prepare.side_effect = None
        self.prepare.return_value = None
        with patch.dict(os.environ, {node_packs.DEPLOYMENT_ENV: "deployment-2"}):
            await restarted.refresh_node_packs()
        self.assertEqual(self.prepare.call_count, 2)
        self.assertEqual(restarted.journal.data[node_packs.REFRESH_KEY]["status"], "unchanged")

    async def test_missing_snapshot_is_repaired_and_failed_first_install_can_retry(self):
        self.prepare.return_value = None
        await self.control.refresh_node_packs()
        self.snapshot.return_value = None
        self.prepare.side_effect = RuntimeError("initial install unavailable")
        for _ in range(2):
            with self.assertLogs("comfy_split.gateway", level="ERROR"):
                await self.control.refresh_node_packs()
        self.assertEqual(self.prepare.call_count, 3)

    async def test_same_revisions_do_not_restart_comfy_or_republish(self):
        self.prepare.return_value = None
        await self.control.refresh_node_packs()
        self.control.candidate.start.assert_not_awaited()
        self.volumes["environment"].commit.aio.assert_not_awaited()

    async def test_failed_volume_commit_does_not_select_unpublished_snapshot(self):
        self.volumes["environment"].commit.aio.side_effect = RuntimeError("commit failed")
        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            await self.control.refresh_node_packs()
        self.assertEqual(self.control.journal.data["environment"], "base")
        self.volumes["data"].commit.aio.assert_not_awaited()

    async def test_catalog_uses_live_node_packs_and_keeps_unrelated_gpu_nodes(self):
        root = Path(self.directory.name)
        write_json(root / "catalog.json", {
            "objects": {
                "RemovedManagedNode": {"python_module": "custom_nodes.ComfyUI-Jev"},
                "GPUOnly": {"python_module": "custom_nodes.user-node"},
            },
            "choice_sources": {"RemovedManagedNode": {"required": {"model": "models"}}},
        })
        response = SimpleNamespace(raise_for_status=Mock(), json=AsyncMock(return_value={
            "CurrentManagedNode": {"python_module": "custom_nodes.ComfyUI-Jev"},
        }))
        context = AsyncMock()
        context.__aenter__.return_value = response
        self.control.client = SimpleNamespace(get=Mock(return_value=context))
        with patch("comfy_split.gateway.environment_path", return_value=root):
            current = await self.control.objects("/object_info")
            self.assertEqual(set(json.loads(current.text)), {"CurrentManagedNode", "GPUOnly"})
            response.json.return_value = {}
            disabled = await self.control.objects("/object_info")
            self.assertEqual(set(json.loads(disabled.text)), {"GPUOnly"})
