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
        comfyapp._reject_dotenv_modal_profile(
            self._dotenv("MODAL_PROFILE=same\n"),
            "same",
        )

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


if __name__ == "__main__":
    unittest.main()
