import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from comfy_split import node_packs, runtime
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
                for role in ("cpu", "gpu"):
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
                                                **provider_keys}), \
                         patch.object(runtime.asyncio, "create_subprocess_exec", launch), \
                         self.assertRaisesRegex(RuntimeError, "captured launch"):
                        await process.start("env-snapshot", cpu=role == "cpu")
                    command = launch.call_args.args
                    self.assertNotIn(node_packs.TOKEN_ENV, launch.call_args.kwargs["env"])
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
