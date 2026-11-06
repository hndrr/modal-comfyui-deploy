import unittest

from comfy_split.config import Settings
from comfy_split.extension_sources import INTEGRATIONS

PLAIN = Settings().environment()
ADAPTER = next(iter(INTEGRATIONS.values()))
ADAPTER_SECRET, ADAPTER_TOKEN = ADAPTER["secrets"][0]


class SettingsTests(unittest.TestCase):
    def test_settings_require_explicit_selection(self):
        self.assertEqual(Settings.read({}), Settings())
        self.assertEqual(Settings.read(PLAIN), Settings())
        ambient = Settings.read({"SPLIT_EXTENSIONS": "ambient"})
        self.assertEqual(ambient, Settings(extensions=("ambient",)))
        for key, value in (("SPLIT_EXTENSIONS", "typo"), ("SPLIT_NODE_PACKS", "typo"),
                           (ADAPTER["setting"], "yes")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Settings.read({key: value})

    def test_removed_ambient_mode_reports_migration_settings(self):
        with self.assertRaisesRegex(ValueError,
                "COMFYUI_AMBIENT_MODE.*SPLIT_EXTENSIONS, SPLIT_NODE_PACKS and SPLIT_AGENT_BRIDGE"):
            Settings.read({"COMFYUI_AMBIENT_MODE": "on"})

    def test_node_packs_and_adapters_do_not_enable_extensions_or_unrelated_secrets(self):
        env = {"SPLIT_NODE_PACKS": "gemini", ADAPTER["setting"]: "on",
               "GEMINI_SECRET_NAME": "gemini", "TYPESAFE_SECRET_NAME": "unused",
               "OPENROUTER_SECRET_NAME": "unused", ADAPTER_SECRET: "adapter"}
        config = Settings.read(env)
        self.assertEqual(config.extensions, ())
        self.assertEqual(config.node_packs, ("gemini",))
        self.assertEqual([name for name, _, _ in config.secrets(env)],
                         ["GEMINI_SECRET_NAME", ADAPTER_SECRET])
