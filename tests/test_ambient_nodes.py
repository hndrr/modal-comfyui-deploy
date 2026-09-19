import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from comfy_split import ambient_nodes, runtime
from comfy_split.gateway import Controller
from comfy_split.state import write_json


class AmbientRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environments = self.root / "environments"
        self.source = self.environments / "base"
        (self.source / "comfy/custom_nodes/user-node").mkdir(parents=True)
        (self.source / "comfy/custom_nodes/user-node/__init__.py").write_text("# user node\n")
        (self.source / "venv/bin").mkdir(parents=True)
        (self.source / "venv/bin/python").symlink_to(sys.executable)
        (self.source / "venv/bin/pip").write_text(f"#!{self.source}/venv/bin/python\n")
        write_json(self.source / "catalog.json", {"objects": {"GPUOnly": {}}})
        self.run = subprocess.run
        self.remotes = {}
        for repo in ambient_nodes.REPOSITORIES:
            name = repo.split("/")[1]
            remote = self.root / name
            self.run(["git", "init", "-q", "-b", "main", str(remote)], check=True)
            (remote / "__init__.py").write_text("# first version\n")
            (remote / "requirements.txt").write_text("jsonschema>=4.23,<5\n")
            self.commit(remote, "initial")
            self.remotes[repo] = remote
        self.calls = []
        self.fail_dependency = False
        self.fail_clone = False
        self.addCleanup(patch.stopall)
        patch.object(runtime, "ENVIRONMENTS", self.environments).start()
        patch.dict(os.environ, {ambient_nodes.TOKEN_ENV: "test-token",
                              "GIT_TRACE_CURL": "1", "GIT_CURL_VERBOSE": "1"}).start()
        patch.object(ambient_nodes.subprocess, "run", side_effect=self.execute).start()

    def commit(self, remote, message):
        self.run(["git", "-C", str(remote), "add", "."], check=True)
        self.run(["git", "-C", str(remote), "-c", "user.name=Test", "-c",
                  "user.email=test@example.invalid", "commit", "-qm", message], check=True)

    def execute(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if command[0] == "git":
            command = list(command)
            if "clone" in command:
                if self.fail_clone:
                    raise subprocess.CalledProcessError(128, command)
                self.assertNotIn("GIT_TRACE_CURL", kwargs["env"])
                self.assertNotIn("GIT_CURL_VERBOSE", kwargs["env"])
                askpass = Path(kwargs["env"]["GIT_ASKPASS"])
                self.assertNotIn("test-token", askpass.read_text())
                response = self.run([str(askpass), "Password for 'https://github.com':"],
                                    env=kwargs["env"], check=True, capture_output=True, text=True)
                self.assertEqual(response.stdout.strip(), "test-token")
                repo = command[-2].removeprefix("https://github.com/").removesuffix(".git")
                command[-2] = self.remotes[repo].as_uri()
            return self.run(command, **kwargs)
        self.assertNotIn(ambient_nodes.TOKEN_ENV, kwargs["env"])
        if self.fail_dependency:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    def test_latest_default_branches_are_published_as_one_immutable_snapshot(self):
        version = ambient_nodes.prepare_environment("base")
        target = self.environments / version
        manifest = json.loads((target / ambient_nodes.MANIFEST).read_text())
        self.assertEqual(set(manifest), ambient_nodes.NODE_NAMES)
        self.assertFalse((self.source / ambient_nodes.DIRECTORY).exists())
        self.assertTrue((target / "comfy/custom_nodes/user-node/__init__.py").exists())
        self.assertEqual(json.loads((target / "catalog.json").read_text()),
                         {"objects": {"GPUOnly": {}}})
        pip = next(command for command, _ in self.calls if "pip" in command)
        self.assertEqual(pip.count("-r"), 4)
        self.assertIn("/opt/split-constraints.txt", pip)
        self.assertIsNone(ambient_nodes.prepare_environment(version))
        self.assertEqual(len(list(self.environments.iterdir())), 2)
        remote = self.remotes[ambient_nodes.REPOSITORIES[0]]
        (remote / "__init__.py").write_text("# latest version\n")
        self.commit(remote, "update")
        updated = ambient_nodes.prepare_environment(version)
        name = remote.name
        self.assertEqual((target / ambient_nodes.DIRECTORY / name / "__init__.py").read_text(),
                         "# first version\n")
        self.assertEqual((self.environments / updated / ambient_nodes.DIRECTORY / name
                          / "__init__.py").read_text(), "# latest version\n")
        self.assertNotEqual(manifest[name], json.loads(
            (self.environments / updated / ambient_nodes.MANIFEST).read_text())[name])
        self.assertTrue(all("test-token" not in " ".join(command) for command, _ in self.calls))

    def test_failed_fetch_or_missing_token_never_copies_the_active_environment(self):
        self.fail_clone = True
        with self.assertRaises(subprocess.CalledProcessError):
            ambient_nodes.prepare_environment("base")
        self.assertEqual(list(self.environments.iterdir()), [self.source])
        with patch.dict(os.environ, {ambient_nodes.TOKEN_ENV: ""}):
            with self.assertRaisesRegex(RuntimeError, ambient_nodes.TOKEN_ENV):
                ambient_nodes.prepare_environment("base")
        self.assertEqual(list(self.environments.iterdir()), [self.source])

    def test_failed_dependency_install_leaves_source_untouched(self):
        self.fail_dependency = True
        with self.assertRaises(subprocess.CalledProcessError):
            ambient_nodes.prepare_environment("base")
        self.assertFalse((self.source / ambient_nodes.MANIFEST).exists())
        self.assertFalse((self.source / ambient_nodes.DIRECTORY).exists())
        self.assertEqual((self.source / "venv/bin/pip").read_text(),
                         f"#!{self.source}/venv/bin/python\n")

    def test_manager_candidate_retains_ambient_snapshot(self):
        version = ambient_nodes.prepare_environment("base")
        candidate = runtime.create_environment(version)
        original = self.environments / version
        copied = self.environments / candidate
        self.assertEqual((copied / ambient_nodes.MANIFEST).read_text(),
                         (original / ambient_nodes.MANIFEST).read_text())
        self.assertEqual({p.name for p in (copied / ambient_nodes.DIRECTORY).iterdir()},
                         ambient_nodes.NODE_NAMES)


class AmbientStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.volumes = {key: SimpleNamespace(commit=SimpleNamespace(aio=AsyncMock()))
                        for key in ("environment", "data")}
        self.worker = Mock()
        self.control = Controller(self.worker, None, None, self.volumes, Path(self.directory.name))
        self.catalog = {"objects": {name: {"python_module": "custom_nodes." + name}
                                    for name in ambient_nodes.NODE_NAMES}}
        self.control.candidate = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(),
                                                catalog=AsyncMock(return_value=self.catalog))
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {ambient_nodes.MODE_ENV: "on"}).start()
        self.prepare = patch.object(ambient_nodes, "prepare_environment", return_value="env-new").start()

    async def test_refresh_commits_snapshot_before_selecting_it_and_never_wakes_gpu(self):
        selected_during_commit = []
        async def commit():
            selected_during_commit.append(self.control.journal.data["environment"])
        self.volumes["environment"].commit.aio.side_effect = commit
        await self.control.refresh_ambient_nodes()
        self.assertEqual(selected_during_commit, ["base"])
        self.assertEqual(self.control.journal.data["environment"], "env-new")
        self.assertEqual(json.loads(self.control.journal.path.read_text())["environment"], "env-new")
        self.control.candidate.start.assert_awaited_once_with("env-new", cpu=True)
        self.assertEqual(self.worker.mock_calls, [])

    async def test_disabled_busy_and_editing_startups_do_not_fetch(self):
        with patch.dict(os.environ, {ambient_nodes.MODE_ENV: "off"}):
            await self.control.refresh_ambient_nodes()
        for status in ("queued", "running", "unknown"):
            self.control.journal.data["jobs"] = {"job": {"status": status}}
            await self.control.refresh_ambient_nodes()
        self.control.journal.data["jobs"] = {}
        self.control.journal.data["candidate"] = {"status": "editing"}
        await self.control.refresh_ambient_nodes()
        self.prepare.assert_not_called()

    async def test_fetch_dependency_and_import_failures_keep_previous_environment(self):
        for error in (RuntimeError("private repository unavailable"),
                      RuntimeError("dependency conflict"), None):
            self.prepare.side_effect = error
            self.control.candidate.catalog.return_value = {"objects": {}}
            with self.assertLogs("comfy_split.gateway", level="ERROR"):
                await self.control.refresh_ambient_nodes()
            self.assertEqual(self.control.journal.data["environment"], "base")
        self.volumes["environment"].commit.aio.assert_not_awaited()
        self.assertEqual(self.worker.mock_calls, [])

    async def test_same_revisions_do_not_restart_comfy_or_republish(self):
        self.prepare.return_value = None
        await self.control.refresh_ambient_nodes()
        self.control.candidate.start.assert_not_awaited()
        self.volumes["environment"].commit.aio.assert_not_awaited()

    async def test_failed_volume_commit_does_not_select_unpublished_snapshot(self):
        self.volumes["environment"].commit.aio.side_effect = RuntimeError("commit failed")
        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            await self.control.refresh_ambient_nodes()
        self.assertEqual(self.control.journal.data["environment"], "base")
        self.volumes["data"].commit.aio.assert_not_awaited()

    async def test_catalog_uses_live_ambient_nodes_and_keeps_unrelated_gpu_nodes(self):
        root = Path(self.directory.name)
        write_json(root / "catalog.json", {
            "objects": {
                "RemovedAmbientNode": {"python_module": "custom_nodes.ComfyUI-Jev"},
                "GPUOnly": {"python_module": "custom_nodes.user-node"},
            },
            "choice_sources": {"RemovedAmbientNode": {"required": {"model": "models"}}},
        })
        response = SimpleNamespace(raise_for_status=Mock(), json=AsyncMock(return_value={
            "CurrentAmbientNode": {"python_module": "custom_nodes.ComfyUI-Jev"},
        }))
        context = AsyncMock()
        context.__aenter__.return_value = response
        self.control.client = SimpleNamespace(get=Mock(return_value=context))
        with patch("comfy_split.gateway.environment_path", return_value=root):
            current = await self.control.objects("/object_info")
            self.assertEqual(set(json.loads(current.text)), {"CurrentAmbientNode", "GPUOnly"})
            response.json.return_value = {}
            disabled = await self.control.objects("/object_info")
            self.assertEqual(set(json.loads(disabled.text)), {"GPUOnly"})


class AmbientLaunchTests(unittest.IsolatedAsyncioTestCase):
    def test_named_provider_secrets_do_not_capture_local_keys(self):
        import comfyapp  # Load shared configuration before spying on this app's secrets.
        import modal

        settings = {
            "GITHUB_SECRET_NAME": "github-for-test",
            "GEMINI_SECRET_NAME": " my-gemini ",
            "TYPESAFE_SECRET_NAME": "",  # An unused provider needs no Secret.
            "OPENROUTER_SECRET_NAME": "my-openrouter",
            "AGENT_RUNTIME_SECRET_NAME": "my-agent-runtime",
            "GEMINI_API_KEY": "local-value-must-not-be-uploaded",
            "TYPESAFE_API_KEY": "local-value-must-not-enable-provider",
            "OPENROUTER_API_KEY": "local-value-must-not-be-uploaded",
            "AGENT_RUNTIME_BRIDGE_TOKEN": "local-bridge-value-must-not-be-uploaded",
        }
        for mode in ("on", "off"):
            with self.subTest(mode=mode), \
                 patch.dict(os.environ, {**settings, ambient_nodes.MODE_ENV: mode}), \
                 patch.object(modal.Secret, "from_name", wraps=modal.Secret.from_name) as named, \
                 patch.object(modal.Secret, "from_dict") as inline:
                app = runpy.run_path(str(Path(comfyapp.__file__).with_name("splitapp.py")))
            inline.assert_not_called()
            if mode == "on":
                self.assertEqual([secret.name for secret in app["ambient_secrets"]],
                                 ["my-gemini", "my-openrouter", "my-agent-runtime"])
                self.assertEqual([(call.args[0], call.kwargs["required_keys"])
                                  for call in named.call_args_list], [
                    ("my-gemini", ["GEMINI_API_KEY"]),
                    ("my-openrouter", ["OPENROUTER_API_KEY"]),
                    ("my-agent-runtime", ["AGENT_RUNTIME_BRIDGE_TOKEN"]),
                    ("github-for-test", ["GITHUB_TOKEN"]),
                ])
            else:
                named.assert_not_called()
                self.assertEqual(app["ambient_secrets"], [])

    def test_container_import_preserves_named_secret_dependencies(self):
        import comfyapp
        import modal

        image_env = {}
        original = modal.Image.env

        def capture(image, values):
            image_env.update(values)
            return original(image, values)

        settings = {ambient_nodes.MODE_ENV: "on", "GITHUB_SECRET_NAME": "private-repos",
                    "GEMINI_SECRET_NAME": "provider-a", "TYPESAFE_SECRET_NAME": "",
                    "OPENROUTER_SECRET_NAME": "provider-b", "AGENT_RUNTIME_SECRET_NAME": "mac-bridge",
                    "GEMINI_API_KEY": "must-not-be-baked", "AGENT_RUNTIME_BRIDGE_TOKEN": "also-private"}
        path = str(Path(comfyapp.__file__).with_name("splitapp.py"))
        with patch.dict(os.environ, settings), patch.object(modal.Image, "env", capture):
            local = runpy.run_path(path)
        self.assertNotIn("must-not-be-baked", json.dumps(image_env))
        self.assertNotIn("also-private", json.dumps(image_env))
        # A container has image env + injected credentials, but no local .env.
        with patch.dict(os.environ, image_env, clear=True):
            remote = runpy.run_path(path)
        for key in ("ambient_secrets", "github_secrets"):
            self.assertEqual([secret.name for secret in remote[key]], [secret.name for secret in local[key]])

    async def test_cpu_and_gpu_use_same_snapshot_only_when_enabled_without_git_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environments = root / "environments"
            source = environments / "env-snapshot"
            (source / "comfy/custom_nodes").mkdir(parents=True)
            (source / ambient_nodes.DIRECTORY).mkdir()
            template = root / "template"
            (template / "custom_nodes").mkdir(parents=True)
            (template / "main.py").write_text("# main\n")
            user = root / "user"
            user.mkdir()
            provider_keys = {"GEMINI_API_KEY": "gemini-from-modal",
                             "TYPESAFE_API_KEY": "typesafe-from-modal",
                             "OPENROUTER_API_KEY": "openrouter-from-modal",
                             "AGENT_RUNTIME_BRIDGE_TOKEN": "bridge-from-modal"}
            def local_path(value):
                return root / value.lstrip("/") if value in {"/models", "/data/input", "/data/output"} else Path(value)
            for mode in ("on", "off"):
                for role in ("cpu", "gpu"):
                    process = runtime.ComfyProcess(role, 8187)
                    process.root = root / f"{mode}-{role}"
                    process.temp_root = process.root / "temporary"
                    process.log = root / f"{mode}-{role}.log"
                    launch = AsyncMock(side_effect=RuntimeError("captured launch"))
                    with patch.object(runtime, "ENVIRONMENTS", environments), \
                         patch.object(runtime, "TEMPLATE", template), patch.object(runtime, "USER", user), \
                         patch.object(runtime, "Path", side_effect=local_path), \
                         patch.dict(os.environ, {ambient_nodes.MODE_ENV: mode,
                                                ambient_nodes.TOKEN_ENV: "not-for-comfy",
                                                **provider_keys}), \
                         patch.object(runtime.asyncio, "create_subprocess_exec", launch):
                        with self.assertRaisesRegex(RuntimeError, "captured launch"):
                            await process.start("env-snapshot", cpu=role == "cpu")
                    command = launch.call_args.args
                    self.assertNotIn(ambient_nodes.TOKEN_ENV, launch.call_args.kwargs["env"])
                    self.assertEqual({key: launch.call_args.kwargs["env"][key] for key in provider_keys},
                                     provider_keys)
                    config = Path(command[command.index("--extra-model-paths-config") + 1])
                    if mode == "on":
                        self.assertEqual(json.loads(config.read_text())["ambient"]["custom_nodes"],
                                         str(source / ambient_nodes.DIRECTORY))
                    else:
                        self.assertEqual(config.name, "extension_paths.yaml")

    def test_mode_defaults_off_and_rejects_typos(self):
        self.assertFalse(ambient_nodes.enabled({}))
        self.assertTrue(ambient_nodes.enabled({ambient_nodes.MODE_ENV: " ON "}))
        with self.assertRaises(ValueError):
            ambient_nodes.enabled({ambient_nodes.MODE_ENV: "of"})
