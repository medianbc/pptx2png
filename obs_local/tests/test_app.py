import asyncio
import configparser
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from obs_local import app


def _hold_category_output_lock(path, acquired, release=None):
    with app._category_output_lock(Path(path)):
        acquired.set()
        if release is not None:
            release.wait(10)


class SlideRangeTests(unittest.TestCase):
    def test_manual_category_range_is_reused_after_changing_selection(self):
        results = {
            "sermon": (140, 140, [140]),
            "opening": (3, 15, [3, 15]),
            "prayer": (None, None, []),
        }
        saved = {}
        with patch("builtins.input", side_effect=["2", "3", "105-107"]):
            self.assertEqual(app._select_range(results, 161, saved), ("prayer", [(105, 107)]))
        with patch("builtins.input", side_effect=["2", "1", "140-150"]):
            app._select_range(results, 161, saved)
        with patch("builtins.input", side_effect=["2", "3", ""]):
            self.assertEqual(app._select_range(results, 161, saved), ("prayer", [(105, 107)]))
        self.assertEqual(saved["sermon"], [(140, 150)])
        with patch("builtins.input", side_effect=["2", "1", ""]):
            self.assertEqual(app._select_range(results, 161), ("sermon", [(140, 140)]))

    def test_multiple_categories_can_be_edited_and_excluded(self):
        ranges = {"sermon": [(140, 140)], "opening": [(3, 15)]}
        selected = set(ranges)
        with patch("builtins.input", side_effect=[
            "3", "105-107", "1", "140-150", "2", "3-10, 12", "5", "2", "4",
        ]):
            result = app._select_categories(ranges, selected, 161)
        self.assertEqual(result, {"sermon": [(140, 150)], "prayer": [(105, 107)]})
        self.assertEqual(ranges["opening"], [(3, 10), (12, 12)])
        with patch("builtins.input", side_effect=["5", "2", "4"]):
            self.assertEqual(set(app._select_categories(ranges, selected, 161)), set(app.CATEGORIES))

    def test_batch_back_keeps_edits_and_invalid_inputs_do_not_change_ranges(self):
        ranges = {}
        selected = set()
        with patch("builtins.input", side_effect=[
            "4", "5", "3", "bad", "3", "162", "107-105", "", "105-107", "0",
        ]):
            self.assertIsNone(app._select_categories(ranges, selected, 161))
        self.assertEqual(ranges, {"prayer": [(105, 107)]})
        with patch("builtins.input", side_effect=["4"]):
            self.assertEqual(app._select_categories(ranges, selected, 161), ranges)

    def test_batch_eof_cancels(self):
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(app.OperationCancelled):
                app._select_categories({}, set(), 161)

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
        with patch("builtins.input", side_effect=["1"]):
            category, ranges = app._select_range({}, 12)

        self.assertEqual((category, ranges), ("all", [(1, 12)]))

    def test_empty_input_uses_all_slides_default(self):
        with patch("builtins.input", return_value=""):
            self.assertEqual(app._select_range({}, 12), ("all", [(1, 12)]))

    def test_category_mode_uses_detected_range_by_default(self):
        results = {
            "sermon": (3, 8, [3, 8]),
            "opening": (None, None, []),
            "prayer": (None, None, []),
        }
        with patch("builtins.input", side_effect=["2", "1", ""]):
            category, ranges = app._select_range(results, 12)

        self.assertEqual((category, ranges), ("sermon", [(3, 8)]))

    def test_category_mode_accepts_a_manual_multi_range(self):
        results = {
            "sermon": (None, None, []),
            "opening": (None, None, []),
            "prayer": (None, None, []),
        }
        with patch("builtins.input", side_effect=["2", "2", "2-4, 7"]):
            category, ranges = app._select_range(results, 12)

        self.assertEqual((category, ranges), ("opening", [(2, 4), (7, 7)]))

    def test_confirmation_can_return_to_selection(self):
        self.assertFalse(
            app._confirm_selection(
                "custom", [(1, 2)], 12, input_fn=lambda _: "2"
            )
        )


class CategoryConversionTests(unittest.TestCase):
    def execute(self, root, answers, page_count=161):
        source = root / "synthetic.pptx"
        source.touch()
        results = {
            "sermon": (140, 140, [140]),
            "opening": (3, 15, [3, 15]),
            "prayer": (None, None, []),
        }

        async def render(_source, render_dir, _quality):
            paths = []
            for index in range(1, page_count + 1):
                path = render_dir / f"slide_{index:03d}.png"
                path.write_bytes(b"synthetic PNG")
                paths.append(path)
            return paths, None

        with (
            patch.object(app, "extract_speaker_notes", return_value=(True, {}, False)),
            patch.object(app, "_find_categories", return_value=results),
            patch.object(app, "Presentation") as presentation,
            patch.object(app, "convert_all_pngs", new_callable=AsyncMock, side_effect=render) as convert,
            patch("builtins.input", side_effect=answers),
        ):
            presentation.return_value.slides = [None] * 161
            output = asyncio.run(app.prepare_obs_folder(
                {
                    "keywords": {},
                    "root_path": root / "output",
                    "category_output_path": root / "output" / "Трансляция",
                    "quality": "standard",
                },
                source, datetime(2026, 11, 8),
            ))
        return output, convert

    def test_edit_single_then_prepare_all_categories_and_render_once(self):
        with tempfile.TemporaryDirectory() as directory:
            output, convert = self.execute(Path(directory), [
                "2", "3", "105-107", "2", "4",
                "1", "140-150", "2", "3-10", "4", "1",
            ])
            self.assertEqual(
                {path.name for path in output.iterdir() if path.is_dir()},
                {"Проповедь", "Начало собрания", "Молитва"},
            )
            self.assertTrue((output / ".category-output.lock").is_file())
            self.assertEqual(len(list((output / "Проповедь").glob("*.png"))), 11)
            self.assertEqual(len(list((output / "Начало собрания").glob("*.png"))), 8)
            self.assertEqual(len(list((output / "Молитва").glob("*.png"))), 3)
            self.assertEqual(output, Path(directory) / "output" / "Трансляция")
            convert.assert_awaited_once()

    def test_confirm_change_keeps_batch_ranges_and_single_category_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output, convert = self.execute(Path(directory), [
                "4", "3", "105-107", "4", "2",
                "5", "1", "5", "2", "4", "1",
            ])
            self.assertEqual(
                {path.name for path in output.iterdir() if path.is_dir()},
                {"Молитва"},
            )
            self.assertEqual(len(list((output / "Молитва").glob("*.png"))), 3)
            convert.assert_awaited_once()
        with tempfile.TemporaryDirectory() as directory:
            output, convert = self.execute(Path(directory), [
                "2", "3", "105-107", "2", "2", "3", "", "1",
            ])
            self.assertEqual(output.name, "Молитва")
            self.assertEqual(output.parent.name, "Трансляция")
            self.assertEqual(len(list(output.glob("*.png"))), 3)
            convert.assert_awaited_once()

    def test_category_reruns_keep_only_the_latest_generated_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, _ = self.execute(
                root, ["2", "3", "105-107", "1"]
            )

            second, _ = self.execute(
                root, ["2", "3", "110-112", "1"]
            )

            backup_root = second.parent / ".backups" / "Молитва"
            user_backup = backup_root / "ручной-архив"
            user_backup.mkdir(parents=True)
            (user_backup / "keep.txt").write_text("keep", encoding="utf-8")

            third, _ = self.execute(
                root, ["2", "3", "120-122", "1"]
            )

            self.assertEqual(first, second)
            self.assertEqual(second, third)
            self.assertEqual(
                {path.name for path in third.glob("*.png")},
                {"slide_120.png", "slide_121.png", "slide_122.png"},
            )
            generated_backups = [
                path
                for path in backup_root.iterdir()
                if path.name != "ручной-архив"
            ]
            self.assertEqual(len(generated_backups), 1)
            self.assertEqual(
                {path.name for path in generated_backups[0].glob("*.png")},
                {"slide_110.png", "slide_111.png", "slide_112.png"},
            )
            self.assertEqual(
                (user_backup / "keep.txt").read_text(encoding="utf-8"), "keep"
            )

    def test_category_cleanup_keeps_backup_created_with_clock_moved_backwards(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory) / "Трансляция"
            target = output_root / "Молитва"
            target.mkdir(parents=True)
            (target / "previous.png").write_bytes(b"previous slides")

            backup_root = output_root / ".backups" / "Молитва"
            future_backup = backup_root / "20261231-235959-000000-aaaaaaaa"
            future_backup.mkdir(parents=True)
            (future_backup / "older.png").write_bytes(b"older backup")

            staging = Path(directory) / "staging"
            staging.mkdir()
            (staging / "current.png").write_bytes(b"current slides")
            with patch.object(
                app,
                "datetime",
                Mock(now=Mock(return_value=datetime(2026, 1, 1))),
            ):
                backups = app._publish_category_outputs(
                    {"prayer": staging}, output_root
                )

            self.assertEqual(
                {path.name for path in target.iterdir()}, {"current.png"}
            )
            self.assertEqual(
                {path.name for path in backups["prayer"].iterdir()},
                {"previous.png"},
            )
            self.assertEqual(
                list(backup_root.iterdir()), [backups["prayer"]]
            )

    def test_batch_copy_failure_does_not_replace_any_live_category(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "output" / "Трансляция"
            sermon = output_root / "Проповедь"
            opening = output_root / "Начало собрания"
            sermon.mkdir(parents=True)
            opening.mkdir()
            (sermon / "old.png").write_bytes(b"old sermon")
            (opening / "old.png").write_bytes(b"old opening")
            original_copy = app.shutil.copy2

            def fail_opening_copy(source, destination, *args, **kwargs):
                if Path(destination).parent.name == "opening":
                    raise OSError("synthetic copy failure")
                return original_copy(source, destination, *args, **kwargs)

            with patch.object(app.shutil, "copy2", side_effect=fail_opening_copy):
                with self.assertRaisesRegex(OSError, "synthetic copy failure"):
                    self.execute(root, ["4", "4", "1"])

            self.assertEqual((sermon / "old.png").read_bytes(), b"old sermon")
            self.assertEqual((opening / "old.png").read_bytes(), b"old opening")
            self.assertFalse((output_root / ".backups").exists())

    def test_batch_publish_failure_restores_all_previous_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "output" / "Трансляция"
            sermon = output_root / "Проповедь"
            opening = output_root / "Начало собрания"
            sermon.mkdir(parents=True)
            opening.mkdir()
            (sermon / "old.png").write_bytes(b"old sermon")
            (opening / "old.png").write_bytes(b"old opening")
            original_rename = Path.rename

            def fail_opening_publish(source, target):
                if (
                    Path(source).name == "opening"
                    and Path(target) == opening
                ):
                    raise OSError("synthetic publish failure")
                return original_rename(source, target)

            with patch.object(Path, "rename", fail_opening_publish):
                with self.assertRaisesRegex(
                    RuntimeError, "предыдущие файлы восстановлены"
                ):
                    self.execute(root, ["4", "4", "1"])

            self.assertEqual((sermon / "old.png").read_bytes(), b"old sermon")
            self.assertEqual((opening / "old.png").read_bytes(), b"old opening")
            self.assertEqual({path.name for path in sermon.iterdir()}, {"old.png"})
            self.assertEqual({path.name for path in opening.iterdir()}, {"old.png"})

    def test_category_output_lock_serializes_processes(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            first_acquired = context.Event()
            release_first = context.Event()
            second_acquired = context.Event()
            first = context.Process(
                target=_hold_category_output_lock,
                args=(directory, first_acquired, release_first),
            )
            second = context.Process(
                target=_hold_category_output_lock,
                args=(directory, second_acquired),
            )
            try:
                first.start()
                self.assertTrue(first_acquired.wait(10))
                second.start()
                self.assertFalse(second_acquired.wait(0.25))
                release_first.set()
                self.assertTrue(second_acquired.wait(10))
            finally:
                release_first.set()
                first.join(10)
                second.join(10)
                if first.is_alive():
                    first.terminate()
                    first.join()
                if second.is_alive():
                    second.terminate()
                    second.join()
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)

    def test_all_slides_keep_run_specific_output_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            output, _ = self.execute(Path(directory), ["1", "1"])

            self.assertEqual(output.name, "all")
            self.assertEqual(output.parent.parent.parent.name, "2026-11-08")
            self.assertEqual(len(list(output.glob("*.png"))), 161)

    def test_cancel_or_missing_rendered_pages_does_not_publish_output(self):
        for answers, pages, error in (
            (["4", "4", "0"], 161, app.OperationCancelled),
            (["4", "4", "1"], 10, RuntimeError),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(error):
                    self.execute(root, answers, page_count=pages)
                self.assertFalse((root / "output").exists())


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

    def test_empty_remote_selection_cancels(self):
        with (
            patch.object(app.aiohttp, "ClientSession", return_value=self.session_context),
            patch.object(app, "YandexDiskClient", return_value=self.client),
            patch.object(app, "resolve_sunday_paths", new_callable=AsyncMock,
                         return_value={"source": "/presentations/source"}),
            patch.object(app, "find_pptx_in_source", new_callable=AsyncMock,
                         return_value=[{"name": "deck.pptx"}]),
        ):
            with self.assertRaises(app.OperationCancelled):
                asyncio.run(app.download_presentation(
                    self.config, datetime(2026, 11, 8), self.temp_dir,
                    input_fn=lambda _: "",
                ))
        self.client.download_file.assert_not_awaited()

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
                    input_fn=Mock(side_effect=["2", str(local_presentation)]),
                )
            )

        self.assertEqual(result, local_presentation)
        self.client.download_file.assert_not_awaited()

    def test_missing_remote_presentation_accepts_a_local_file(self):
        paths = {"source": "/presentations/service"}
        local_presentation = self.temp_dir / "local deck.pptx"
        local_presentation.write_bytes(b"synthetic pptx placeholder")
        escaped_path = '"' + str(local_presentation).replace(
            " ", r"\ " if os.name != "nt" else " "
        ) + '" '
        with (
            patch.object(
                app.aiohttp, "ClientSession", return_value=self.session_context
            ),
            patch.object(app, "YandexDiskClient", return_value=self.client),
            patch.object(
                app,
                "resolve_sunday_paths",
                new_callable=AsyncMock,
                return_value=paths,
            ),
            patch.object(
                app,
                "find_pptx_in_source",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = asyncio.run(
                app.download_presentation(
                    self.config,
                    datetime(2026, 11, 8),
                    self.temp_dir,
                    input_fn=Mock(side_effect=["2", escaped_path]),
                )
            )

        self.assertEqual(result, local_presentation)
        self.client.download_file.assert_not_awaited()

    def test_numeric_choice_retries_yandex_search(self):
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
            answers = iter(["1", "1"])
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
                        input_fn=lambda _: "0",
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
            answers = iter(["/disk/presentations", "", "invalid", "2"])

            with (
                patch.object(app, "BASE_DIR", obs_dir),
                patch.object(app, "PROJECT_DIR", project_dir),
            ):
                config = app.load_config(
                    interactive=True,
                    input_fn=lambda _: next(answers),
                    secret_input_fn=lambda _: "test-token",
                )

            saved_settings = app.get_settings_path(obs_dir).read_text(encoding="utf-8")
            secret_path = obs_dir / "config.ini"
            self.assertIn("# Keep user comments.", saved_settings)
            self.assertIn("# Keep section comment.", saved_settings)
            self.assertIn("base_path = /disk/presentations", saved_settings)
            self.assertEqual(saved_settings.count("[YandexDisk]"), 1)
            self.assertIn("root_path = ./obs_png", saved_settings)
            self.assertIn("quality = 2k", saved_settings)
            self.assertNotIn("quality = invalid", saved_settings)
            self.assertEqual(config["root_path"], project_dir / "obs_png")
            self.assertEqual(
                config["category_output_path"], project_dir / "obs_png" / "Трансляция"
            )
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
        self.assertEqual(
            config["category_output_path"], project_dir / "obs_png" / "Трансляция"
        )

    def test_category_output_path_can_be_relative_to_root_or_absolute(self):
        for configured, expected in (
            ("Категории OBS", "configured-root/Категории OBS"),
            ("/shared/OBS/Трансляция", "/shared/OBS/Трансляция"),
        ):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as directory:
                project_dir = Path(directory)
                obs_dir = project_dir / "obs_local"
                obs_dir.mkdir()
                (obs_dir / "settings.ini").write_text(
                    "[YandexDisk]\nbase_path = /disk\n"
                    "[OBS Local paths]\n"
                    "root_path = configured-root\n"
                    f"category_output_path = {configured}\n"
                    "quality = standard\n",
                    encoding="utf-8",
                )
                (project_dir / "config.ini").write_text(
                    "[YandexDisk]\ntoken = test-token\n", encoding="utf-8"
                )
                with (
                    patch.object(app, "BASE_DIR", obs_dir),
                    patch.object(app, "PROJECT_DIR", project_dir),
                ):
                    config = app.load_config()
                expected_path = Path(expected)
                if not expected_path.is_absolute():
                    expected_path = project_dir / expected_path
                self.assertEqual(config["category_output_path"], expected_path)


if __name__ == "__main__":
    unittest.main()
