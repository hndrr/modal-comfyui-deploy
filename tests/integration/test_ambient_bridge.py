"""The two real adapters coexist without enabling one another or node packs."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from comfy_split.config import Settings
from comfy_split.extensions import check_requirements
from comfy_split.gateway import Controller


class ExtensionMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_gateway_node_and_relay_selections(self):
        for ambient in ("", "ambient"):
            for bridge in ("off", "on"):
                for nodes in ("", "agent-bridge", "agent-runtime"):
                    with self.subTest(ambient=ambient, bridge=bridge, nodes=nodes), TemporaryDirectory() as root, \
                         patch.dict(os.environ, {"SPLIT_EXTENSIONS": ambient, "SPLIT_AGENT_BRIDGE": bridge,
                                                 "SPLIT_NODE_PACKS": nodes}):
                        control = Controller(None, None, None, {}, Path(root))
                        names = {type(plugin).__name__ for plugin in control.plugins}
                        self.assertEqual("SplitAmbient" in names, bool(ambient))
                        self.assertEqual("SplitBridge" in names, bridge == "on")
                        self.assertEqual(Settings.read().node_packs, (nodes,) if nodes else ())
                        body = {"prompt": {"1": {"class_type": "AgentRuntimeBridgeText"}}}
                        if bridge == "on":
                            check_requirements(body)
                        else:
                            with self.assertRaises(ValueError):
                                check_requirements(body)
                        for plugin in control.plugins:
                            await plugin.close()
