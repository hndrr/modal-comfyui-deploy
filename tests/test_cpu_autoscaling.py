import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from comfy_split.gateway import Controller


def remote():
    return SimpleNamespace(aio=AsyncMock())


class CpuAutoscalingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ui = SimpleNamespace(update_autoscaler=remote())
        self.volumes = {key: SimpleNamespace(commit=remote()) for key in ('data', 'environment')}
        self.control = Controller(None, None, None, self.volumes, Path(self.directory.name),
                                  ui_function=self.ui)

    async def test_queue_pins_before_commit_and_completion_releases(self):
        order = []
        async def scale(**kwargs):
            order.append(('scale', kwargs['min_containers']))
        async def commit():
            order.append(('commit', None))
        self.ui.update_autoscaler.aio.side_effect = scale
        self.volumes['data'].commit.aio.side_effect = commit
        job = self.control.journal.enqueue({'prompt': {'1': {}}})
        await self.control.persist()
        self.assertEqual(order, [('scale', 1), ('commit', None)])
        await self.control.reconcile_cpu_scaling()
        self.assertEqual(len(order), 2)
        job['status'] = 'completed'
        await self.control.persist()
        await self.control.reconcile_cpu_scaling()
        self.assertEqual(order[-2:], [('commit', None), ('scale', 0)])

    async def test_failed_pin_does_not_acknowledge_work(self):
        self.control.journal.enqueue({'prompt': {'1': {}}})
        self.ui.update_autoscaler.aio.side_effect = RuntimeError('unavailable')
        with self.assertRaises(RuntimeError):
            await self.control.persist()
        self.volumes['data'].commit.aio.assert_not_awaited()
        self.assertFalse(self.control.journal.path.exists())

    async def test_recovered_running_job_keeps_cpu_without_resubmission(self):
        job = self.control.journal.enqueue({'prompt': {'1': {}}})
        job.update(status='running', call_id='fc-existing')
        self.control.journal.save()
        restored = Controller(None, None, None, self.volumes, Path(self.directory.name),
                              ui_function=self.ui)
        restored.journal.recover()
        await restored.reconcile_cpu_scaling()
        self.ui.update_autoscaler.aio.assert_awaited_once_with(min_containers=1)
        self.assertIsNone(restored.journal.next_job())

    async def test_idle_and_failed_maintenance_release_cpu(self):
        for candidate in (None, {'status': 'failed'}):
            self.control.cpu_pinned = True
            self.control.journal.data['candidate'] = candidate
            await self.control.reconcile_cpu_scaling()
            self.assertFalse(self.control.cpu_pinned)
        self.control.journal.data['session'] = {'operation': 'legacy'}
        await self.control.reconcile_cpu_scaling()
        self.assertTrue(self.control.cpu_pinned)

    async def test_manager_finishes_and_commits_before_sleep(self):
        self.control.journal.data['candidate'] = {'status': 'editing'}
        self.control.candidate.process = SimpleNamespace(returncode=None)
        response = SimpleNamespace(raise_for_status=Mock(), json=AsyncMock(
            return_value={'is_processing': True, 'pending_count': 0}))
        context = AsyncMock()
        context.__aenter__.return_value = response
        self.control.client = SimpleNamespace(get=Mock(return_value=context))
        await self.control.reconcile_cpu_scaling()
        self.assertTrue(self.control.cpu_pinned)
        response.json.return_value = {'is_processing': False, 'pending_count': 0}
        self.volumes['environment'].commit.aio.side_effect = RuntimeError('commit failed')
        with self.assertRaises(RuntimeError):
            await self.control.reconcile_cpu_scaling()
        self.assertTrue(self.control.cpu_pinned)
        self.volumes['environment'].commit.aio.side_effect = None
        await self.control.reconcile_cpu_scaling()
        self.assertFalse(self.control.cpu_pinned)
