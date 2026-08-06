import os
import unittest
from unittest.mock import patch

import preserve_model


class AppCompositionTests(unittest.TestCase):
    def test_download_and_web_functions_are_deployed_as_one_app(self) -> None:
        self.assertEqual(
            {"preserve_model", "web"},
            set(preserve_model.app.registered_functions),
        )


class ResolveIntEnvTests(unittest.TestCase):
    CONFIGS = (
        (
            preserve_model.PRESERVE_WEB_SCALEDOWN_WINDOW_ENV,
            preserve_model.DEFAULT_SCALEDOWN_WINDOW,
            preserve_model.MIN_SCALEDOWN_WINDOW,
            preserve_model.MAX_SCALEDOWN_WINDOW,
        ),
        (
            preserve_model.PRESERVE_WEB_FUNCTION_TIMEOUT_ENV,
            preserve_model.DEFAULT_FUNCTION_TIMEOUT,
            preserve_model.MIN_FUNCTION_TIMEOUT,
            preserve_model.MAX_FUNCTION_TIMEOUT,
        ),
    )

    def test_uses_default_when_environment_variable_is_missing(self) -> None:
        for env_name, default, minimum, maximum in self.CONFIGS:
            with self.subTest(env_name=env_name):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(env_name, None)
                    self.assertEqual(
                        preserve_model._resolve_int_env(
                            env_name, default, minimum, maximum
                        ),
                        default,
                    )

    def test_accepts_boundaries_and_whitespace(self) -> None:
        for env_name, default, minimum, maximum in self.CONFIGS:
            for value in (minimum, maximum):
                with self.subTest(env_name=env_name, value=value):
                    with patch.dict(os.environ, {env_name: f"  {value}  "}):
                        self.assertEqual(
                            preserve_model._resolve_int_env(
                                env_name, default, minimum, maximum
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
                            preserve_model._resolve_int_env(
                                env_name, default, minimum, maximum
                            )


class ResolveOnOffEnvTests(unittest.TestCase):
    ENV_NAME = preserve_model.PRESERVE_WEB_REQUIRES_PROXY_AUTH_ENV

    def test_defaults_to_off_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.ENV_NAME, None)
            self.assertFalse(preserve_model._resolve_on_off_env(self.ENV_NAME))

    def test_accepts_on_and_off_with_whitespace_and_case(self) -> None:
        cases = (
            ("on", True),
            ("ON", True),
            ("  on  ", True),
            ("off", False),
            ("OFF", False),
            ("  off  ", False),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                with patch.dict(os.environ, {self.ENV_NAME: value}):
                    self.assertEqual(
                        preserve_model._resolve_on_off_env(self.ENV_NAME),
                        expected,
                    )

    def test_rejects_other_values(self) -> None:
        for value in ("", " ", "1", "true", "yes", "enabled"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {self.ENV_NAME: value}):
                    with self.assertRaisesRegex(ValueError, self.ENV_NAME):
                        preserve_model._resolve_on_off_env(self.ENV_NAME)


class ImageSourceLayoutTests(unittest.TestCase):
    """preserve_model_gui.py は preserve_model.py を隣のファイルとして読むため、
    イメージ内でも同じディレクトリに置かれている必要がある。"""

    def test_both_sources_land_in_the_same_remote_directory(self) -> None:
        remote_paths = [
            f"{preserve_model.REMOTE_SOURCE_DIR}/preserve_model.py",
            f"{preserve_model.REMOTE_SOURCE_DIR}/preserve_model_gui.py",
        ]
        directories = {path.rsplit("/", 1)[0] for path in remote_paths}
        self.assertEqual(len(directories), 1)


if __name__ == "__main__":
    unittest.main()
