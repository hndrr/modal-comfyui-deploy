"""Image contents, immutable pins, and build/runtime credential separation."""
import json
import os
import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from comfy_split.config import NODE_PACKS, Settings
from comfy_split.release import REVISION_ENV
from comfy_split.extension_sources import (
    EXTENSIONS,
    INTEGRATIONS,
    NODE_SOURCES,
    node_revision,
)

ROOT = Path(__file__).resolve().parents[2]
PLAIN = Settings().environment()
ADAPTER = next(iter(INTEGRATIONS.values()))
ADAPTER_SECRET, ADAPTER_TOKEN = ADAPTER["secrets"][0]


class DeploymentTests(unittest.TestCase):
    def test_cpu_execution_worker_has_no_gpu_and_shares_worker_resources(self):
        import modal
        import comfyapp

        definitions = {}
        original = modal.App.function

        def capture(app, *args, **kwargs):
            decorate = original(app, *args, **kwargs)
            def register(fn):
                if hasattr(fn, "__name__"):
                    definitions[fn.__name__] = kwargs
                return decorate(fn)
            return register

        with (patch.dict(os.environ, PLAIN), patch.object(modal.App, "function", capture),
              patch("comfy_split.release.comfy_revision", return_value="a" * 40)):
            runpy.run_path(str(Path(comfyapp.__file__).with_name("splitapp.py")))
        cpu, gpu = definitions["cpu_worker"], definitions["gpu_worker"]
        self.assertNotIn("gpu", cpu)
        self.assertEqual((cpu["cpu"], cpu["memory"]), (2, 8192))
        for field in ("secrets", "volumes", "retries", "timeout", "min_containers", "max_containers", "scaledown_window"):
            self.assertEqual(cpu[field], gpu[field])
        self.assertEqual((cpu["min_containers"], cpu["max_containers"], cpu["scaledown_window"]), (0, 1, 30))
        self.assertNotIn("enable_memory_snapshot", cpu)

    def test_gpu_verification_uses_dotenv_revision_without_overriding_shell(self):
        """Verify fresh imports use the dotenv SHA while preserving a shell override."""
        script = '''
import os, runpy, sys
from io import StringIO
from unittest.mock import patch
from dotenv import load_dotenv
from comfy_split.release import REVISION_ENV
with (
    patch('dotenv.load_dotenv', side_effect=lambda path, **kwargs:
          load_dotenv(stream=StringIO(f'{REVISION_ENV}={"a" * 40}'), **kwargs)),
    patch('comfy_split.release.github_json', side_effect=AssertionError('Unexpected GitHub lookup')),
):
    runpy.run_path('scripts/verify_split_gpus.py')
    import splitapp
    assert splitapp.COMFY_REVISION == sys.argv[1]
'''
        for shell_sha in (None, "b" * 40):
            with self.subTest(shell_sha=shell_sha):
                env = {**os.environ, **PLAIN}
                env.pop(REVISION_ENV, None)
                if shell_sha is not None:
                    env[REVISION_ENV] = shell_sha
                result = subprocess.run([sys.executable, "-c", script, shell_sha or "a" * 40],
                    cwd=ROOT, env=env, text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_plain_deployment_never_imports_or_bundles_extensions_or_requests_secrets(self):
        """Keep the plain image independent of optional integrations and their credentials."""
        script = '''
import importlib.abc, json, runpy, sys
from unittest.mock import patch
from tests.isolation import ForbidOptionalExtensions
sys.meta_path.insert(0, ForbidOptionalExtensions())
import comfyapp, modal
from comfy_split import gateway, runtime, cpu_snapshot, worker
sources = []
original = modal.Image.add_local_dir
def record(image, source, *args, **kwargs):
    sources.append(str(source))
    return original(image, source, *args, **kwargs)
with (
    patch('comfy_split.release.comfy_revision', return_value='a' * 40),
    patch.object(modal.Secret, 'from_name', side_effect=AssertionError('Unexpected Secret')),
    patch.object(modal.Image, 'add_local_dir', record),
    patch.object(modal.Image, 'pip_install_private_repos', side_effect=AssertionError('Unexpected private repository')),
):
    app = runpy.run_path('splitapp.py')
assert app['settings'].extensions == ()
assert app['provider_secrets'] == app['github_secrets'] == []
assert set(sources) == {'comfy_split', 'extensions/ComfyUI-Modal-Control', 'extensions/ComfyUI-Modal-Bridge'}
# Runtime tests run once, under the same import guard in test_standalone.py.
print(json.dumps({'standalone': True}))
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
            env={**os.environ, **PLAIN}, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"standalone": true', result.stdout)

    def test_named_provider_secrets_do_not_capture_local_keys(self):
        """Include only enabled providers' named secrets, never local credential values."""
        import modal

        import comfyapp  # Load shared configuration before spying on this app's secrets.

        settings = {
            "GITHUB_SECRET_NAME": "github-for-test",
            "GEMINI_SECRET_NAME": " my-gemini ",
            "TYPESAFE_SECRET_NAME": "",  # An unused provider needs no Secret.
            "OPENROUTER_SECRET_NAME": "my-openrouter",
            ADAPTER_SECRET: "my-adapter",
            "GEMINI_API_KEY": "local-value-must-not-be-uploaded",
            "TYPESAFE_API_KEY": "local-value-must-not-enable-provider",
            "OPENROUTER_API_KEY": "local-value-must-not-be-uploaded",
            ADAPTER_TOKEN: "local-adapter-value-must-not-be-uploaded",
        }
        for enabled in (True, False):
            selection = (Settings(node_packs=tuple(NODE_PACKS), integrations=tuple(INTEGRATIONS))
                         if enabled else Settings())
            with self.subTest(enabled=enabled), \
                 patch("comfy_split.release.comfy_revision", return_value="a" * 40), \
                 patch.dict(os.environ, {**settings, **selection.environment()}), \
                 patch.object(modal.Secret, "from_name", wraps=modal.Secret.from_name) as named, \
                 patch.object(modal.Secret, "from_dict") as inline:
                app = runpy.run_path(str(Path(comfyapp.__file__).with_name("splitapp.py")))
            inline.assert_not_called()
            if enabled:
                self.assertEqual([secret.name for secret in app["provider_secrets"]],
                                 ["my-gemini", "my-openrouter", "my-adapter"])
                self.assertEqual([(call.args[0], call.kwargs["required_keys"])
                                  for call in named.call_args_list], [
                    ("my-gemini", ["GEMINI_API_KEY"]),
                    ("my-openrouter", ["OPENROUTER_API_KEY"]),
                    ("my-adapter", [ADAPTER_TOKEN]),
                    ("github-for-test", ["GITHUB_TOKEN"]),
                ])
            else:
                named.assert_not_called()
                self.assertEqual(app["provider_secrets"], [])

    def test_container_import_preserves_named_secret_dependencies(self):
        """Preserve release and secret identities at cold start while allowing a new deployment."""
        import modal

        import comfyapp

        image_env = {}
        image_updates = []
        original = modal.Image.env

        def capture(image, values):
            image_env.update(values)
            image_updates.append(values)
            return original(image, values)

        settings = {**Settings(node_packs=tuple(NODE_PACKS), integrations=tuple(INTEGRATIONS)).environment(),
                    REVISION_ENV: "",
                    "GITHUB_SECRET_NAME": "private-repos",
                    "GEMINI_SECRET_NAME": "provider-a", "TYPESAFE_SECRET_NAME": "",
                    "OPENROUTER_SECRET_NAME": "provider-b", ADAPTER_SECRET: "adapter",
                    "GEMINI_API_KEY": "must-not-be-baked", ADAPTER_TOKEN: "also-private"}
        path = str(Path(comfyapp.__file__).with_name("splitapp.py"))
        commits = ("a" * 40, "b" * 40)
        def deploy_release(sha):
            return patch("comfy_split.release.github_json", side_effect=[
                {"tag_name": "v1", "draft": False, "prerelease": False}, {"sha": sha}])

        with patch.dict(os.environ, settings), patch.object(modal.Image, "env", capture), deploy_release(commits[0]):
            local = runpy.run_path(path)
        self.assertEqual(image_env[REVISION_ENV], commits[0])
        self.assertNotIn("must-not-be-baked", json.dumps(image_env))
        self.assertNotIn("also-private", json.dumps(image_env))
        for field, expected in (("SPLIT_GEMINI_BILLING_LABEL", "provider-a"),
                                ("SPLIT_OPENROUTER_BILLING_LABEL", "provider-b")):
            # Both CPU and GPU image layers must carry the same billing label.
            labels = [values[field] for values in image_updates if field in values]
            self.assertEqual(labels, [expected, expected])
        # A container has image env + injected credentials, but no local .env.
        # Import as a GPU container; the CPU snapshot additionally preloads its gateway.
        image_env.pop("SPLIT_CPU_MEMORY_SNAPSHOT", None)
        with patch.dict(os.environ, image_env, clear=True), patch.object(modal, "is_local", return_value=False), \
             patch("comfy_split.release.github_json", side_effect=AssertionError("Cold start contacted GitHub")):
            remote = runpy.run_path(path)
        self.assertEqual(remote["COMFY_REVISION"], local["COMFY_REVISION"])
        self.assertEqual(remote["DEPLOYMENT_ID"], local["DEPLOYMENT_ID"])
        with patch.dict(os.environ, settings), deploy_release(commits[1]):
            redeployed = runpy.run_path(path)
        self.assertEqual(redeployed["COMFY_REVISION"], commits[1])
        self.assertNotEqual(redeployed["DEPLOYMENT_ID"], local["DEPLOYMENT_ID"])
        for key in ("provider_secrets", "github_secrets"):
            self.assertEqual([secret.name for secret in remote[key]], [secret.name for secret in local[key]])

    def test_registered_sources_have_immutable_pins(self):
        for name, source in (*EXTENSIONS.items(), *INTEGRATIONS.items()):
            with self.subTest(extension=name):
                self.assertRegex(source["revision"], r"^[0-9a-f]{40}$")
                self.assertNotEqual(source["revision"], "0" * 40)
                self.assertTrue(source["distribution"])
                self.assertTrue(source["module"])
                self.assertTrue(source["factory"])

    def test_selected_packages_share_build_path_and_limit_runtime_credentials(self):
        """Install selected private packages at immutable revisions with only their required secrets."""
        import modal

        import comfyapp  # noqa: F401 - initialize the base image before spying on split's install
        for name, source in (*EXTENSIONS.items(), *INTEGRATIONS.items()):
            environment = {**Settings().environment(), "GITHUB_SECRET_NAME": "extension-reader"}
            if "setting" in source:
                environment[source["setting"]] = "on"
            else:
                environment["SPLIT_EXTENSIONS"] = name
            secrets = {key: "runtime-secret-" + str(i) for i, (key, _) in enumerate(source.get("secrets", []))}
            with self.subTest(extension=name), patch.dict(os.environ, {**environment, **secrets}), \
                 patch("comfy_split.release.comfy_revision", return_value="a" * 40), \
                 patch.object(modal.Image, "pip_install_private_repos", autospec=True,
                              side_effect=lambda image, *a, **kw: image) as install:
                app = runpy.run_path(str(ROOT / "splitapp.py"))
            self.assertEqual(app["github_secrets"], [])
            self.assertEqual([s.name for s in app["provider_secrets"]], list(secrets.values()))
            self.assertEqual(install.call_count, 1)
            self.assertEqual(install.call_args.args[1:],
                             (f"github.com/{source['repository']}@{source['revision']}",))
            self.assertEqual([secret.name for secret in install.call_args.kwargs["secrets"]], ["extension-reader"])
            self.assertEqual(install.call_args.kwargs["extra_options"], "--no-deps")

    def test_referenced_nodes_share_their_adapter_pin(self):
        for source in INTEGRATIONS.values():
            if source in NODE_SOURCES.values():
                self.assertEqual(node_revision(source["repository"]), source["revision"])
