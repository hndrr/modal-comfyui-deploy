"""Shared boundary cases for the two deployment entrypoints' integer settings."""
import os
from unittest.mock import patch


class IntegerEnvChecks:
    # A mixin, not a TestCase: only the concrete entrypoints are discovered.
    def test_uses_default_when_environment_variable_is_missing(self):
        for name, default, minimum, maximum in self.CONFIGS:
            with self.subTest(setting=name), patch.dict(os.environ):
                os.environ.pop(name, None)
                self.assertEqual(self.resolve(name, default, minimum, maximum), default)

    def test_accepts_boundaries_and_whitespace(self):
        for name, default, minimum, maximum in self.CONFIGS:
            for value in (minimum, maximum):
                with self.subTest(setting=name, value=value), patch.dict(os.environ, {name: f"  {value}  "}):
                    self.assertEqual(self.resolve(name, default, minimum, maximum), value)

    def test_rejects_empty_non_numeric_and_out_of_range_values(self):
        for name, default, minimum, maximum in self.CONFIGS:
            for value in ("", " ", "not-a-number", "1.5", "0", "-1", str(minimum - 1), str(maximum + 1)):
                with (self.subTest(setting=name, value=value), patch.dict(os.environ, {name: value}),
                      self.assertRaisesRegex(ValueError, rf"{name}.*{minimum}.*{maximum}")):
                    self.resolve(name, default, minimum, maximum)
