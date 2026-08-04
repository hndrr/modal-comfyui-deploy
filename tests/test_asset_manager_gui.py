from __future__ import annotations

import unittest
from pathlib import Path


class DeprecatedGuiEntrypointTests(unittest.TestCase):
    def test_gradio_entrypoint_exits_with_migration_message(self) -> None:
        import asset_manager_gui

        with self.assertRaises(SystemExit) as raised:
            asset_manager_gui.main()
        self.assertEqual(raised.exception.code, 2)

    def test_web_package_exists(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "web" / "package.json").is_file())
        self.assertTrue((root / "web" / "server" / "app.ts").is_file())
        self.assertTrue((root / "web" / "src" / "App.tsx").is_file())


if __name__ == "__main__":
    unittest.main()
