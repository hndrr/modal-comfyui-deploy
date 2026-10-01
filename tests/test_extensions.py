"""Lifecycle selection and worker wrapping work for any registered adapter."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from comfy_split import extensions
from comfy_split.config import Settings


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = {
            name: {'module': name, 'factory': 'Adapter', 'worker': 'run', 'host': 'capabilities',
                   'setting': 'TEST_' + name.upper(), 'prefix': '/' + name + '/',
                   'node_prefix': name.capitalize(), 'record_key': name + '_state'}
            for name in ('first', 'second')
        }
        self.patch = patch.dict(extensions.INTEGRATIONS, self.catalog, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = patch.dict(os.environ, {**Settings().environment(), 'TEST_FIRST': 'on', 'TEST_SECOND': 'on'})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_worker_wrappers_nest_in_registration_order(self):
        events = []
        def module(name):
            async def run(spec, host, generate):
                events.append(name + ':start')
                result = await generate()
                events.append(name + ':end')
                return result
            return SimpleNamespace(run=run)
        async def generate():
            events.append('generate')
            return 'result'
        with patch.object(extensions, 'import_module', side_effect=module):
            self.assertEqual(await extensions.run_worker_extensions({'body': {}}, object(), generate), 'result')
        self.assertEqual(events, ['first:start', 'second:start', 'generate', 'second:end', 'first:end'])

    async def test_disabled_required_adapter_rejects_without_import_or_gpu_execution(self):
        generate = AsyncMock()
        for spec in ({'body': {'prompt': {'1': {'class_type': 'FirstText'}}}}, {'first_state': 'saved-session'}):
            with self.subTest(spec=spec), patch.dict(os.environ, {'TEST_FIRST': 'off', 'TEST_SECOND': 'off'}), \
                 patch.object(extensions, 'import_module') as imported:
                with self.assertRaisesRegex(ValueError, 'TEST_FIRST'):
                    await extensions.run_worker_extensions(spec, object(), generate)
                imported.assert_not_called()
        generate.assert_not_awaited()
        self.assertTrue(extensions.reserved_route('/first/catalog'))
        self.assertFalse(extensions.reserved_route('/ordinary/catalog'))

    async def test_gateway_receives_capabilities_and_disabled_adapters_are_not_loaded(self):
        factory = Mock()
        controller = SimpleNamespace(journal=SimpleNamespace(data={'jobs': {}, 'mode': 'split', 'candidate': None}))
        with patch.dict(os.environ, {'TEST_SECOND': 'off'}), \
             patch.object(extensions, 'import_module', return_value=SimpleNamespace(Adapter=factory)) as imported:
            extensions.load_extensions(controller)
        imported.assert_called_once_with('first')
        host = factory.call_args.args[0]
        self.assertIsInstance(host, extensions.GatewayHost)
        self.assertIs(host.jobs, controller.journal.data['jobs'])
