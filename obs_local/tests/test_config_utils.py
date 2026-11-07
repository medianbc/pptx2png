import configparser
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from obs_local import config_utils


class PlatformSettingsTests(unittest.TestCase):
    def test_each_platform_gets_independent_settings_and_log_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "settings.ini"
            original = (
                "# Preserve operator settings.\n"
                "[OBS Local paths]\nroot_path = ./custom_png\nquality = 2k\n"
                "[Song preparation]\nlocal_pptx_dir = ./custom_songs\n"
                "[Logging]\nlog_dir = /dev/shm/pptx2png_tasks/obs_local/logs\n"
            )
            legacy.write_text(original, encoding="utf-8")
            platforms = (
                ("linux", "linux", Path("/dev/shm/pptx2png_tasks/obs_local/logs")),
                ("darwin", "macos", root / "Library/Logs/pptx2png/obs_local"),
                ("win32", "windows", root / "AppData/Local/pptx2png/obs_local/logs"),
            )
            with (
                patch.object(Path, "home", return_value=root),
                patch.dict(os.environ, {"LOCALAPPDATA": ""}),
            ):
                for platform, name, expected_log_dir in platforms:
                    with self.subTest(platform=platform), patch.object(
                        config_utils.sys, "platform", platform
                    ):
                        path = config_utils.get_settings_path(root)
                        self.assertEqual(path.name, f"settings.{name}.ini")
                        settings = configparser.ConfigParser()
                        settings.read(path, encoding="utf-8")
                        self.assertEqual(Path(settings["Logging"]["log_dir"]), expected_log_dir)
                        self.assertEqual(settings["OBS Local paths"]["root_path"], "./custom_png")
                        self.assertEqual(settings["OBS Local paths"]["quality"], "2k")
                        self.assertIn("# Preserve", path.read_text(encoding="utf-8"))
                        config_utils.update_ini_file(
                            path, {"OBS Local paths": {"quality": "4k"}}
                        )
                        self.assertEqual(config_utils.get_settings_path(root), path)
                        self.assertIn("quality = 4k", path.read_text(encoding="utf-8"))
            self.assertEqual(legacy.read_text(encoding="utf-8"), original)

    def test_existing_platform_settings_do_not_read_legacy_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.ini").write_text("invalid legacy", encoding="utf-8")
            path = root / "settings.macos.ini"
            path.write_text("[Logging]\nlog_dir = ./my_logs\n", encoding="utf-8")
            with patch.object(config_utils.sys, "platform", "darwin"):
                self.assertEqual(config_utils.get_settings_path(root), path)
            self.assertIn("./my_logs", path.read_text(encoding="utf-8"))

    def test_custom_log_directory_is_preserved_during_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.ini").write_text(
                "[Logging]\nlog_dir = ./custom_logs\n", encoding="utf-8"
            )
            with patch.object(config_utils.sys, "platform", "darwin"):
                path = config_utils.get_settings_path(root)
            self.assertIn("./custom_logs", path.read_text(encoding="utf-8"))

    def test_invalid_legacy_file_is_not_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.ini").write_text("invalid", encoding="utf-8")
            with patch.object(config_utils.sys, "platform", "darwin"):
                with self.assertRaises(configparser.Error):
                    config_utils.get_settings_path(root)
            self.assertFalse((root / "settings.macos.ini").exists())

    def test_windows_uses_local_app_data_environment(self):
        with (
            patch.object(config_utils.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": "/synthetic/local-data"}),
        ):
            self.assertEqual(
                config_utils.default_log_dir(),
                Path("/synthetic/local-data/pptx2png/obs_local/logs"),
            )
