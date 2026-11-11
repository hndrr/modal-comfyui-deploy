import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

DEPLOY_FILE = Path(__file__).resolve().parents[2] / "comfy_split" / "split_guis.py"


class SplitGuiDeploymentTests(unittest.TestCase):
    def load(self, origin, *, local=True):
        with patch.dict(os.environ, {"SPLIT_GUI_PUBLIC_ORIGIN": origin}), patch("modal.is_local", return_value=local):
            return runpy.run_path(str(DEPLOY_FILE))

    def test_local_deploy_rejects_missing_or_non_origin_urls(self):
        for value in ("", "http://split.example.com", "https://", "https://user@split.example.com",
                      "https://split.example.com/path", "https://split.example.com?x",
                      "https://split.example.com/#fragment", "https://split.example.com:99999"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "SPLIT_GUI_PUBLIC_ORIGIN"):
                self.load(value)

    def test_deploy_accepts_https_origin_with_trailing_slash(self):
        self.assertEqual(self.load("https://split.example.com:8443/")["public_url"],
                         "https://split.example.com:8443")

    def test_remote_assets_container_can_import_without_models_origin(self):
        self.assertEqual(self.load("", local=False)["public_url"], "")
