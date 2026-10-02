import unittest

from comfy_split.config import NODE_PACKS, Settings
from comfy_split.extension_sources import INTEGRATIONS, LEGACY

PLAIN = Settings().environment()
ADAPTER = next(iter(INTEGRATIONS.values()))
ADAPTER_SECRET, ADAPTER_TOKEN = ADAPTER["secrets"][0]


class SettingsTests(unittest.TestCase):
    def test_new_settings_override_legacy_individually_including_empty(self):
        self.assertEqual(Settings.read({}), Settings())
        old = {LEGACY["mode_env"]: "on"}
        self.assertEqual(Settings.read(old).node_packs, tuple(NODE_PACKS))
        self.assertEqual(Settings.read({**old, **PLAIN}), Settings())
        self.assertEqual(Settings.read({**old, "SPLIT_EXTENSIONS": ""}).extensions, ())
        for key, value in (("SPLIT_EXTENSIONS", "typo"), ("SPLIT_NODE_PACKS", "typo"),
                           (ADAPTER["setting"], "yes"), (LEGACY["mode_env"], "of")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Settings.read({key: value})
        self.assertEqual(Settings.read({LEGACY["mode_env"]: " ON "}).node_packs, tuple(NODE_PACKS))

    def test_node_packs_and_adapters_do_not_enable_extensions_or_unrelated_secrets(self):
        env = {"SPLIT_NODE_PACKS": "gemini", ADAPTER["setting"]: "on",
               "GEMINI_SECRET_NAME": "gemini", "TYPESAFE_SECRET_NAME": "unused",
               "OPENROUTER_SECRET_NAME": "unused", ADAPTER_SECRET: "adapter"}
        config = Settings.read(env)
        self.assertEqual(config.extensions, ())
        self.assertEqual(config.node_packs, ("gemini",))
        self.assertEqual([name for name, _, _ in config.secrets(env)],
                         ["GEMINI_SECRET_NAME", ADAPTER_SECRET])
