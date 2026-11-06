import asyncio
import configparser
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from obs_local import app


class SlideRangeTests(unittest.TestCase):
    def test_parses_and_merges_single_slides_and_ranges(self):
        self.assertEqual(
            app._parse_slide_ranges("1, 3-5, 5–7, 10", 12),
            [(1, 1), (3, 7), (10, 10)],
        )

    def test_rejects_malformed_and_out_of_bounds_ranges(self):
        for value in ("", "1,,3", "0", "4-2", "1-13", "one"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app._parse_slide_ranges(value, 12)

    def test_all_slides_mode_returns_full_range(self):
        with patch("builtins.input", side_effect=["all"]):
            category, ranges = app._select_range({}, 12)

        self.assertEqual((category, ranges), ("all", [(1, 12)]))

    def test_category_mode_uses_detected_range_by_default(self):
        results = {
            "sermon": (3, 8, [3, 8]),
            "opening": (None, None, []),
            "prayer": (None, None, []),
        }
        with patch("builtins.input", side_effect=["category", "1", ""]):
            category, ranges = app._select_range(results, 12)

        self.assertEqual((category, ranges), ("sermon", [(3, 8)]))

    def test_category_mode_accepts_a_manual_multi_range(self):
        results = {
            "sermon": (None, None, []),
            "opening": (None, None, []),
            "prayer": (None, None, []),
        }
        with patch("builtins.input", side_effect=["category", "2", "2-4, 7"]):
            category, ranges = app._select_range(results, 12)

        self.assertEqual((category, ranges), ("opening", [(2, 4), (7, 7)]))

    def test_confirmation_can_return_to_selection(self):
        self.assertFalse(
            app._confirm_selection(
                "custom", [(1, 2)], 12, input_fn=lambda _: "e"
            )
        )


class PresentationSourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_context = tempfile.TemporaryDirectory(
            dir="/dev/shm" if Path("/dev/shm").is_dir() else None
        )
        self.addCleanup(self.temp_context.cleanup)
        self.temp_dir = Path(self.temp_context.name)
        self.config = {
            "token": "test-token",
            "base_path": "/presentations",
            "source_folder": "Служение",
        }
        self.session = object()
        self.session_context = MagicMock()
        self.session_context.__aenter__ = AsyncMock(return_value=self.session)
        self.session_context.__aexit__ = AsyncMock(return_value=False)
        self.client = MagicMock()
        self.client.check_access = AsyncMock(return_value=(True, ""))
        self.client.download_file = AsyncMock(return_value=True)

    def test_missing_remote_folder_accepts_a_local_presentation(self):
        local_presentation = self.temp_dir / "local-deck.pptx"
        local_presentation.write_bytes(b"synthetic pptx placeholder")
        with (
            patch.object(
                app.aiohttp, "ClientSession", return_value=self.session_context
            ),
            patch.object(app, "YandexDiskClient", return_value=self.client),
            patch.object(
                app,
                "resolve_sunday_paths",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = asyncio.run(
                app.download_presentation(
                    self.config,
                    datetime(2026, 11, 8),
                    self.temp_dir,
                    input_fn=lambda _: str(local_presentation),
                )
            )

        self.assertEqual(result, local_presentation)
        self.client.download_file.assert_not_awaited()

    def test_enter_retries_yandex_search(self):
        paths = {"source": "/presentations/source"}
        presentation = {
            "name": "remote-deck.pptx",
            "path": "/presentations/source/remote-deck.pptx",
        }
        with (
            patch.object(
                app.aiohttp, "ClientSession", return_value=self.session_context
            ),
            patch.object(app, "YandexDiskClient", return_value=self.client),
            patch.object(
                app,
                "resolve_sunday_paths",
                new_callable=AsyncMock,
                side_effect=[None, paths],
            ) as resolve_paths,
            patch.object(
                app,
                "find_pptx_in_source",
                new_callable=AsyncMock,
                return_value=[presentation],
            ),
        ):
            answers = iter(["", "1"])
            result = asyncio.run(
                app.download_presentation(
                    self.config,
                    datetime(2026, 11, 8),
                    self.temp_dir,
                    input_fn=lambda _: next(answers),
                )
            )

        self.assertEqual(resolve_paths.await_count, 2)
        self.client.download_file.assert_awaited_once_with(
            presentation["path"], self.temp_dir / presentation["name"]
        )
        self.assertEqual(result, self.temp_dir / presentation["name"])

    def test_exit_choice_cancels_without_ending_the_process(self):
        with (
            patch.object(
                app.aiohttp, "ClientSession", return_value=self.session_context
            ),
            patch.object(app, "YandexDiskClient", return_value=self.client),
            patch.object(
                app,
                "resolve_sunday_paths",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            with self.assertRaises(app.OperationCancelled):
                asyncio.run(
                    app.download_presentation(
                        self.config,
                        datetime(2026, 11, 8),
                        self.temp_dir,
                        input_fn=lambda _: "exit",
                    )
                )


class InteractiveConfigTests(unittest.TestCase):
    def test_missing_settings_and_token_are_prompted_and_saved_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            obs_dir = project_dir / "obs_local"
            obs_dir.mkdir()
            settings_path = obs_dir / "settings.ini"
            settings_path.write_text(
                "# Keep user comments.\n"
                "[YandexDisk] # Keep section comment.\n"
                "base_path: \n"
                "source_folder = Meeting\n",
                encoding="utf-8",
            )
            answers = iter(["/disk/presentations", "", "invalid", "2k"])

            with (
                patch.object(app, "BASE_DIR", obs_dir),
                patch.object(app, "PROJECT_DIR", project_dir),
            ):
                config = app.load_config(
                    interactive=True,
                    input_fn=lambda _: next(answers),
                    secret_input_fn=lambda _: "test-token",
                )

            saved_settings = settings_path.read_text(encoding="utf-8")
            secret_path = obs_dir / "config.ini"
            self.assertIn("# Keep user comments.", saved_settings)
            self.assertIn("# Keep section comment.", saved_settings)
            self.assertIn("base_path = /disk/presentations", saved_settings)
            self.assertEqual(saved_settings.count("[YandexDisk]"), 1)
            self.assertIn("root_path = ./obs_png", saved_settings)
            self.assertIn("quality = 2k", saved_settings)
            self.assertNotIn("quality = invalid", saved_settings)
            self.assertEqual(config["root_path"], project_dir / "obs_png")
            self.assertEqual(config["quality"], "2k")
            self.assertEqual(config["token"], "test-token")
            self.assertIn("token = test-token", secret_path.read_text(encoding="utf-8"))
            if os.name == "posix":
                self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_updated_settings_are_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.ini"
            original = (
                "[YandexDisk]\n"
                "base_path = /first\n"
                "base_path: /second\n"
            )
            settings_path.write_text(original, encoding="utf-8")

            with self.assertRaises(configparser.DuplicateOptionError):
                app._update_ini_file(
                    settings_path,
                    {"YandexDisk": {"base_path": "/updated"}},
                )

            self.assertEqual(settings_path.read_text(encoding="utf-8"), original)

    def test_non_interactive_config_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            obs_dir = project_dir / "obs_local"
            obs_dir.mkdir()
            (obs_dir / "settings.ini").write_text(
                "[YandexDisk]\nbase_path = /disk\n"
                "[OBS Local paths]\nroot_path = ./obs_png\nquality = standard\n",
                encoding="utf-8",
            )
            (obs_dir / "config.ini").write_text(
                "[YandexDisk]\ntoken = local-token\n",
                encoding="utf-8",
            )

            with (
                patch.object(app, "BASE_DIR", obs_dir),
                patch.object(app, "PROJECT_DIR", project_dir),
            ):
                config = app.load_config()

        self.assertEqual(config["token"], "local-token")
        self.assertEqual(config["base_path"], "/disk")


if __name__ == "__main__":
    unittest.main()
