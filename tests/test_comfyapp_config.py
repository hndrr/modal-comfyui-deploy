import configparser
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import comfyapp


class ResolveIntEnvTests(unittest.TestCase):
    CONFIGS = (
        (
            comfyapp.COMFYUI_SCALEDOWN_WINDOW_ENV,
            comfyapp.DEFAULT_SCALEDOWN_WINDOW,
            comfyapp.MIN_SCALEDOWN_WINDOW,
            comfyapp.MAX_SCALEDOWN_WINDOW,
        ),
        (
            comfyapp.COMFYUI_FUNCTION_TIMEOUT_ENV,
            comfyapp.DEFAULT_FUNCTION_TIMEOUT,
            comfyapp.MIN_FUNCTION_TIMEOUT,
            comfyapp.MAX_FUNCTION_TIMEOUT,
        ),
    )

    def test_uses_default_when_environment_variable_is_missing(self) -> None:
        for env_name, default, minimum, maximum in self.CONFIGS:
            with self.subTest(env_name=env_name):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(env_name, None)
                    self.assertEqual(
                        comfyapp._resolve_int_env(
                            env_name,
                            default,
                            minimum,
                            maximum,
                        ),
                        default,
                    )

    def test_accepts_boundaries_and_whitespace(self) -> None:
        for env_name, default, minimum, maximum in self.CONFIGS:
            for value in (minimum, maximum):
                with self.subTest(env_name=env_name, value=value):
                    with patch.dict(os.environ, {env_name: f"  {value}  "}):
                        self.assertEqual(
                            comfyapp._resolve_int_env(
                                env_name,
                                default,
                                minimum,
                                maximum,
                            ),
                            value,
                        )

    def test_rejects_empty_non_numeric_and_out_of_range_values(self) -> None:
        for env_name, default, minimum, maximum in self.CONFIGS:
            invalid_values = (
                "",
                " ",
                "not-a-number",
                "1.5",
                "0",
                "-1",
                str(minimum - 1),
                str(maximum + 1),
            )
            for value in invalid_values:
                with self.subTest(env_name=env_name, value=value):
                    with patch.dict(os.environ, {env_name: value}):
                        with self.assertRaisesRegex(
                            ValueError,
                            rf"{env_name}.*{minimum}.*{maximum}",
                        ):
                            comfyapp._resolve_int_env(
                                env_name,
                                default,
                                minimum,
                                maximum,
                            )


class RejectDotenvModalProfileTests(unittest.TestCase):
    """`.env` の MODAL_PROFILE は modal に届かないので、黙って通してはいけない。"""

    def _dotenv(self, body: str) -> Path:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / ".env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_raises_when_dotenv_overrides_the_shell(self) -> None:
        for body in (
            "MODAL_PROFILE=other\n",
            "export MODAL_PROFILE=other\n",
            '  MODAL_PROFILE = "other" \n',
            "COMFYUI_GPU_PROFILE=rtx-pro-6000\nMODAL_PROFILE='other'\n",
        ):
            with self.subTest(body=body):
                with self.assertRaisesRegex(RuntimeError, "MODAL_PROFILE=other"):
                    comfyapp._reject_dotenv_modal_profile(self._dotenv(body), None)

        with self.assertRaisesRegex(RuntimeError, "MODAL_PROFILE=other"):
            comfyapp._reject_dotenv_modal_profile(
                self._dotenv("MODAL_PROFILE=other\n"),
                "shell-profile",
            )

    def test_allows_dotenv_that_matches_the_shell(self) -> None:
        # Inline comments belong to the .env syntax, not to the value.
        for body in (
            "MODAL_PROFILE=same\n",
            "MODAL_PROFILE=same # note\n",
            'MODAL_PROFILE="same"\n',
            "export MODAL_PROFILE=same\n",
        ):
            with self.subTest(body=body):
                comfyapp._reject_dotenv_modal_profile(self._dotenv(body), "same")

    def test_ignores_comments_empty_values_and_missing_files(self) -> None:
        for body in (
            "# MODAL_PROFILE=other\n",
            "MODAL_PROFILE=\n",
            "MODAL_PROFILE_EXTRA=other\n",
            "COMFYUI_CLI_ARGS=\n",
            "",
        ):
            with self.subTest(body=body):
                comfyapp._reject_dotenv_modal_profile(self._dotenv(body), None)

        comfyapp._reject_dotenv_modal_profile(Path("/nonexistent/.env"), None)


class PatchWebsocketCompressionTests(unittest.TestCase):
    """Modal のプロキシは permessage-deflate 非対応。合意させてはいけない。"""

    HANDLER = "            ws = web.WebSocketResponse()\n            await ws.prepare(request)\n"

    def _comfy_root(self, server_source: str | None) -> Path:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        root = Path(directory)
        if server_source is not None:
            (root / "server.py").write_text(server_source, encoding="utf-8")
        return root

    def test_disables_compression(self) -> None:
        root = self._comfy_root(self.HANDLER)

        comfyapp.patch_websocket_compression(root)

        patched = (root / "server.py").read_text(encoding="utf-8")
        self.assertIn("web.WebSocketResponse(compress=False)", patched)
        self.assertIn("await ws.prepare(request)", patched)

    def test_is_idempotent(self) -> None:
        already = self.HANDLER.replace(
            "web.WebSocketResponse()", "web.WebSocketResponse(compress=False)"
        )
        root = self._comfy_root(already)

        comfyapp.patch_websocket_compression(root)

        self.assertEqual((root / "server.py").read_text(encoding="utf-8"), already)

    def test_warns_and_keeps_file_when_pattern_is_gone(self) -> None:
        source = "            ws = SomeOtherSocket()\n"
        root = self._comfy_root(source)

        with patch("builtins.print") as printed:
            comfyapp.patch_websocket_compression(root)

        self.assertEqual((root / "server.py").read_text(encoding="utf-8"), source)
        self.assertTrue(
            any("見つからず" in str(call) for call in printed.call_args_list),
            "パターン消失時は警告を出すこと",
        )

    def test_ignores_missing_server_file(self) -> None:
        comfyapp.patch_websocket_compression(self._comfy_root(None))


class ConfigureManagerInstallTests(unittest.TestCase):
    """Manager v4 は network_mode=personal_cloud のときだけノードを入れられる。"""

    def _comfy_root(self, config_body: str | None = None) -> Path:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        root = Path(directory)
        if config_body is not None:
            config_path = root / "user" / "__manager" / "config.ini"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(config_body, encoding="utf-8")
        return root

    def _config(self, root: Path) -> configparser.ConfigParser:
        parser = configparser.ConfigParser()
        parser.read(root / "user" / "__manager" / "config.ini", encoding="utf-8")
        return parser

    def test_enabling_writes_personal_cloud(self) -> None:
        root = self._comfy_root()

        comfyapp.configure_manager_install(root, True)

        parser = self._config(root)
        self.assertEqual(parser.get("default", "network_mode"), "personal_cloud")
        self.assertEqual(parser.get("default", "security_level"), "normal")

    def test_disabling_writes_back_public(self) -> None:
        root = self._comfy_root("[default]\nnetwork_mode = personal_cloud\n")

        comfyapp.configure_manager_install(root, False)

        self.assertEqual(self._config(root).get("default", "network_mode"), "public")

    def test_disabling_does_not_create_a_config(self) -> None:
        root = self._comfy_root()

        comfyapp.configure_manager_install(root, False)

        self.assertFalse((root / "user" / "__manager" / "config.ini").exists())

    def test_keeps_unrelated_settings(self) -> None:
        root = self._comfy_root(
            "[default]\nfile_logging = False\nchannel_url = https://example.com\n"
        )

        comfyapp.configure_manager_install(root, True)

        parser = self._config(root)
        self.assertEqual(parser.get("default", "file_logging"), "False")
        self.assertEqual(parser.get("default", "channel_url"), "https://example.com")
        self.assertEqual(parser.get("default", "network_mode"), "personal_cloud")

    def test_respects_explicit_security_level_but_warns(self) -> None:
        root = self._comfy_root("[default]\nsecurity_level = strong\n")

        with patch("builtins.print") as printed:
            comfyapp.configure_manager_install(root, True)

        self.assertEqual(self._config(root).get("default", "security_level"), "strong")
        self.assertTrue(
            any("security_level=strong" in str(c) for c in printed.call_args_list),
            "インストールできない設定なら警告すること",
        )


class ResolveManagerInstallEnabledTests(unittest.TestCase):
    def test_defaults_to_off(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(comfyapp.COMFYUI_MANAGER_INSTALL_ENV, None)
            self.assertFalse(comfyapp._resolve_manager_install_enabled())

    def test_accepts_on_and_off(self) -> None:
        for raw, expected in (("on", True), ("OFF", False), (" On ", True)):
            with self.subTest(raw=raw):
                with patch.dict(
                    os.environ, {comfyapp.COMFYUI_MANAGER_INSTALL_ENV: raw}
                ):
                    self.assertIs(comfyapp._resolve_manager_install_enabled(), expected)

    def test_rejects_other_values(self) -> None:
        with patch.dict(
            os.environ, {comfyapp.COMFYUI_MANAGER_INSTALL_ENV: "personal_cloud"}
        ):
            with self.assertRaisesRegex(ValueError, "Allowed values: on, off"):
                comfyapp._resolve_manager_install_enabled()


if __name__ == "__main__":
    unittest.main()
