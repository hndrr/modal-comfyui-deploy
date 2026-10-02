"""Lifecycle selection and worker wrapping work for any registered adapter."""
import os
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace
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


class FrontendTests(unittest.TestCase):
    def test_frontend_install_copies_assets_without_registering_native_nodes(self):
        import tempfile

        from comfy_split import extension_frontend
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'package'
            (package / 'assets').mkdir(parents=True)
            (package / 'assets/panel.js').write_text('export const panel = true;')
            target = root / 'extensions'
            target.mkdir()
            catalog = {'sample': {'web_package': 'test_plugin', 'web_directory': 'assets', 'web_name': 'Sample'}}
            with patch.dict(extension_frontend.EXTENSIONS, catalog, clear=True), \
                 patch.object(extension_frontend, 'files', return_value=package) as resources:
                extension_frontend.install('sample', target)
            resources.assert_called_once_with('test_plugin')
            registered = runpy.run_path(str(target / 'Sample/__init__.py'))
            self.assertEqual(registered['NODE_CLASS_MAPPINGS'], {})
            self.assertEqual(registered['WEB_DIRECTORY'], './web')
            self.assertEqual((target / 'Sample/web/panel.js').read_bytes(),
                             (package / 'assets/panel.js').read_bytes())
