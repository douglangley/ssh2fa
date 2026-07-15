#!/usr/bin/env python3
"""Regression tests for Config's bounded-integer validation."""

import tempfile
import unittest

from pam_ssh_2fa import CODE_LENGTH_MAX, CODE_LENGTH_MIN, Config, DEFAULTS


def write_config(path, text):
    with open(path, "w") as f:
        f.write(text)


class ConfigBoundedIntTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def _config_path(self, text):
        path = f"{self.tempdir.name}/config.ini"
        write_config(path, text)
        return path

    def test_valid_code_length_within_range_is_applied(self):
        path = self._config_path("[codes]\nlength = 8\n")
        config = Config(path)
        self.assertEqual(config.get("code_length"), 8)

    def test_code_length_below_minimum_falls_back_to_default(self):
        path = self._config_path(f"[codes]\nlength = {CODE_LENGTH_MIN - 1}\n")
        config = Config(path)
        self.assertEqual(config.get("code_length"), DEFAULTS["code_length"])

    def test_code_length_above_maximum_falls_back_to_default(self):
        path = self._config_path(f"[codes]\nlength = {CODE_LENGTH_MAX + 1}\n")
        config = Config(path)
        self.assertEqual(config.get("code_length"), DEFAULTS["code_length"])

    def test_non_numeric_code_length_falls_back_to_default(self):
        path = self._config_path("[codes]\nlength = six\n")
        config = Config(path)
        self.assertEqual(config.get("code_length"), DEFAULTS["code_length"])

    def test_ratelimit_settings_are_parsed_when_valid(self):
        path = self._config_path(
            "[ratelimit]\n"
            "window = 120\n"
            "max_per_user = 10\n"
            "max_per_rhost = 30\n"
            "max_concurrent_per_user = 2\n"
        )
        config = Config(path)
        self.assertEqual(config.get("ratelimit_window"), 120)
        self.assertEqual(config.get("ratelimit_max_per_user"), 10)
        self.assertEqual(config.get("ratelimit_max_per_rhost"), 30)
        self.assertEqual(config.get("ratelimit_max_concurrent_per_user"), 2)

    def test_zero_or_negative_ratelimit_values_fall_back_to_default(self):
        path = self._config_path("[ratelimit]\nmax_per_user = 0\n")
        config = Config(path)
        self.assertEqual(
            config.get("ratelimit_max_per_user"),
            DEFAULTS["ratelimit_max_per_user"],
        )

    def test_user_config_cannot_override_ratelimit_settings(self):
        # Per-user config files may only touch notification/auth settings;
        # a compromised or careless per-user file must not be able to
        # loosen system-wide rate limits.
        config_dir = self.tempdir.name
        write_config(f"{config_dir}/config.ini", "[ratelimit]\nmax_per_user = 5\n")

        import os

        os.makedirs(f"{config_dir}/users")
        write_config(
            f"{config_dir}/users/attacker.conf",
            "[ratelimit]\nmax_per_user = 100000\n",
        )

        config = Config(f"{config_dir}/config.ini", user="attacker")
        self.assertEqual(config.get("ratelimit_max_per_user"), 5)


if __name__ == "__main__":
    unittest.main()
