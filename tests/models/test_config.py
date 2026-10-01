import os
import unittest
from unittest.mock import patch

import preserve_model
from tests.env_checks import IntegerEnvChecks


class AppCompositionTests(unittest.TestCase):
    def test_download_and_web_functions_are_deployed_as_one_app(self) -> None:
        self.assertEqual(
            {"preserve_model", "web"},
            set(preserve_model.app.registered_functions),
        )


class ResolveIntEnvTests(IntegerEnvChecks, unittest.TestCase):
    resolve = staticmethod(preserve_model._resolve_int_env)
    CONFIGS = (
        (preserve_model.PRESERVE_WEB_SCALEDOWN_WINDOW_ENV, preserve_model.DEFAULT_SCALEDOWN_WINDOW,
         preserve_model.MIN_SCALEDOWN_WINDOW, preserve_model.MAX_SCALEDOWN_WINDOW),
        (preserve_model.PRESERVE_WEB_FUNCTION_TIMEOUT_ENV, preserve_model.DEFAULT_FUNCTION_TIMEOUT,
         preserve_model.MIN_FUNCTION_TIMEOUT, preserve_model.MAX_FUNCTION_TIMEOUT),
    )


class ResolveOnOffEnvTests(unittest.TestCase):
    ENV_NAME = preserve_model.PRESERVE_WEB_REQUIRES_PROXY_AUTH_ENV

    def test_defaults_to_on_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.ENV_NAME, None)
            self.assertTrue(preserve_model._resolve_on_off_env(self.ENV_NAME))

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
            with self.subTest(value=value), patch.dict(os.environ, {self.ENV_NAME: value}):
                self.assertEqual(preserve_model._resolve_on_off_env(self.ENV_NAME), expected)

    def test_rejects_other_values(self) -> None:
        for value in ("", " ", "1", "true", "yes", "enabled"):
            with (self.subTest(value=value), patch.dict(os.environ, {self.ENV_NAME: value}),
                  self.assertRaisesRegex(ValueError, self.ENV_NAME)):
                preserve_model._resolve_on_off_env(self.ENV_NAME)
