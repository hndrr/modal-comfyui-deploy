import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from comfy_split import node_packs, runtime
from comfy_split.gateway_access import ENV as GATEWAY_TOKEN_ENV
from comfy_split.config import NODE_PACKS, Settings
from comfy_split.extension_sources import INTEGRATIONS

ADAPTER_TOKEN = next(iter(INTEGRATIONS.values()))["secrets"][0][1]


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cpu_and_gpu_use_same_snapshot_only_when_enabled_without_git_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environments = root / "environments"
            source = environments / "env-snapshot"
            (source / "comfy/custom_nodes").mkdir(parents=True)
            (source / node_packs.DIRECTORY).mkdir()
            for name in node_packs.NODE_NAMES:
                (source / node_packs.DIRECTORY / name).mkdir()
            template = root / "template"
            (template / "custom_nodes").mkdir(parents=True)
            (template / "main.py").write_text("# main\n")
            user = root / "user"
            user.mkdir()
            provider_keys = {"GEMINI_API_KEY": "gemini-from-modal",
                             "TYPESAFE_API_KEY": "typesafe-from-modal",
                             "OPENROUTER_API_KEY": "openrouter-from-modal",
                             ADAPTER_TOKEN: "adapter-from-modal"}
            def local_path(value):
                return root / value.lstrip("/") if value in {"/models", "/data/input", "/data/output"} else Path(value)
            for mode in ("on", "off", "gemini"):
                packs = tuple(NODE_PACKS) if mode == "on" else (("gemini",) if mode == "gemini" else ())
                selection = Settings(node_packs=packs).environment()
                for role in ("cpu", "candidate", "gpu", "cpu_worker"):
                    process = runtime.ComfyProcess(role, 8187)
                    process.root = root / f"{mode}-{role}"
                    process.temp_root = process.root / "temporary"
                    process.log = root / f"{mode}-{role}.log"
                    launch = AsyncMock(side_effect=RuntimeError("captured launch"))
                    with patch.object(runtime, "ENVIRONMENTS", environments), \
                         patch.object(runtime, "TEMPLATE", template), patch.object(runtime, "USER", user), \
                         patch.object(runtime, "Path", side_effect=local_path), \
                         patch.dict(os.environ, {**selection,
                                                node_packs.TOKEN_ENV: "not-for-comfy",
                                                GATEWAY_TOKEN_ENV: "not-for-comfy",
                                                **provider_keys}), \
                         patch.object(runtime.asyncio, "create_subprocess_exec", launch), \
                         self.assertRaisesRegex(RuntimeError, "captured launch"):
                        await process.start("env-snapshot", cpu=role != "gpu")
                    command = launch.call_args.args
                    self.assertEqual("--cpu" in command, role != "gpu")
                    self.assertEqual(launch.call_args.kwargs["env"]["SPLIT_CPU"],
                                     "1" if role in {"cpu", "candidate"} else "0")
                    self.assertNotIn(node_packs.TOKEN_ENV, launch.call_args.kwargs["env"])
                    self.assertNotIn(GATEWAY_TOKEN_ENV, launch.call_args.kwargs["env"])
                    self.assertEqual({key: launch.call_args.kwargs["env"][key] for key in provider_keys},
                                     provider_keys)
                    config = Path(command[command.index("--extra-model-paths-config") + 1])
                    if mode != "off":
                        self.assertEqual(json.loads(config.read_text())["managed"]["custom_nodes"],
                                         str(process.root / "managed-nodes"))
                        selected = node_packs.NODE_NAMES if mode == "on" else {"ComfyUI-GeminiTools"}
                        self.assertEqual({p.name for p in (process.root / "managed-nodes").iterdir()}, selected)
                        for name in selected:
                            self.assertEqual((process.root / "managed-nodes" / name).resolve(),
                                             (source / node_packs.DIRECTORY / name).resolve())
                    else:
                        self.assertEqual(config.name, "extension_paths.yaml")

                    if mode == "off" and role in {"candidate", "cpu_worker"}:
                        for guard in (False, True):
                            with self.subTest(role=role, guard=guard):
                                process.version = None
                                process.process = None
                                response = AsyncMock()
                                response.__aenter__.return_value = SimpleNamespace(
                                    status=200, json=AsyncMock(return_value={"cpu_guard": guard}))
                                client = AsyncMock()
                                client.__aenter__.return_value = client
                                client.get = Mock(return_value=response)
                                with (patch.object(runtime, "ENVIRONMENTS", environments),
                                      patch.object(runtime, "TEMPLATE", template),
                                      patch.object(runtime, "USER", user),
                                      patch.object(runtime, "Path", side_effect=local_path),
                                      patch.dict(os.environ, selection),
                                      patch.object(runtime.asyncio, "create_subprocess_exec",
                                                   AsyncMock(return_value=SimpleNamespace(returncode=None))),
                                      patch.object(runtime, "ClientSession", return_value=client),
                                      patch.object(process, "stop", AsyncMock())):
                                    if guard == (role == "candidate"):
                                        await process.start("env-snapshot", cpu=True)
                                        self.assertEqual(process.version, "env-snapshot")
                                    else:
                                        expected = ("CPU execution guard did not load" if role == "candidate"
                                                    else "Execution worker unexpectedly has the CPU guard")
                                        with self.assertRaisesRegex(RuntimeError, expected):
                                            await process.start("env-snapshot", cpu=True)
