import unittest
import asyncio
import configparser
import contextlib
import io
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from obs_local import menu


class MainMenuTests(unittest.TestCase):
    def test_routes_to_each_workflow_and_exits(self):
        answers = iter(["invalid", "1", "2", "1", "0", "0"])

        with (
            patch.object(menu, "_prepare_presentation") as prepare_presentation,
            patch.object(menu, "_prepare_songs") as prepare_songs,
        ):
            menu.run_menu(input_fn=lambda _: next(answers))

        prepare_presentation.assert_called_once_with()
        prepare_songs.assert_called_once_with(console_debug=False)

    def test_eof_exits_cleanly(self):
        menu.run_menu(input_fn=Mock(side_effect=EOFError))

    def test_cancelling_presentation_returns_to_menu_for_song_preparation(self):
        answers = iter(["1", "2", "1", "0", "0"])
        with (
            patch.object(
                menu.app,
                "run",
                side_effect=menu.app.OperationCancelled,
            ),
            patch.object(menu, "_prepare_songs") as prepare_songs,
        ):
            menu.run_menu(input_fn=lambda _: next(answers))

        prepare_songs.assert_called_once_with(console_debug=False)

    def test_date_argument_keeps_direct_presentation_launch(self):
        with (
            patch("sys.argv", ["obs_local", "--date", "2026-11-08"]),
            patch.object(menu.app, "main") as app_main,
            patch.object(menu, "run_menu") as run_menu,
        ):
            menu.main()

        app_main.assert_called_once_with()
        run_menu.assert_not_called()

    def test_debug_flag_is_forwarded_to_song_preparation_menu(self):
        with (
            patch("sys.argv", ["obs_local", "--debug"]),
            patch.object(menu, "run_menu") as run_menu,
        ):
            menu.main()

        run_menu.assert_called_once_with(debug=True)

    def test_debug_is_rejected_with_direct_presentation_date(self):
        with (
            patch("sys.argv", ["obs_local", "--date", "2026-11-08", "--debug"]),
            patch.object(menu.app, "main") as app_main,
        ):
            with self.assertRaises(SystemExit) as error:
                menu.main()

        self.assertEqual(error.exception.code, 2)
        app_main.assert_not_called()

    def test_update_check_can_install_and_exits_menu_afterwards(self):
        answers = iter(["3", "6", "1"])
        update = menu.updater.UpdateInfo(
            branch="main",
            local_revision="a" * 40,
            remote_revision="b" * 40,
            available=True,
            is_git_checkout=False,
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(menu, "get_settings_path", return_value=Path(directory) / "settings.ini"),
            patch.object(menu.updater, "check_for_updates", return_value=update),
            patch.object(menu.updater, "install_update") as install_update,
        ):
            (Path(directory) / "settings.ini").write_text(
                "[OBS Local paths]\nquality = standard\n", encoding="utf-8",
            )
            menu.run_menu(input_fn=lambda _: next(answers))

        install_update.assert_called_once_with(update)

    def test_update_check_can_be_cancelled(self):
        answers = iter(["3", "6", "0", "0", "0"])
        update = menu.updater.UpdateInfo(
            branch="main",
            local_revision="a" * 40,
            remote_revision="b" * 40,
            available=True,
            is_git_checkout=False,
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(menu, "get_settings_path", return_value=Path(directory) / "settings.ini"),
            patch.object(menu.updater, "check_for_updates", return_value=update),
            patch.object(menu.updater, "install_update") as install_update,
        ):
            (Path(directory) / "settings.ini").write_text(
                "[OBS Local paths]\nquality = standard\n", encoding="utf-8",
            )
            menu.run_menu(input_fn=lambda _: next(answers))

        install_update.assert_not_called()

    def test_settings_returns_after_update_check_failure_or_no_update(self):
        for error in (None, menu.updater.UpdateError("network unavailable")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.ini"
                path.write_text("[OBS Local paths]\nquality = standard\n", encoding="utf-8")
                update = menu.updater.UpdateInfo(
                    branch="main", local_revision="a" * 40,
                    remote_revision="a" * 40, available=False,
                    is_git_checkout=False,
                )
                answers = iter(["6", "0"])
                with (
                    patch.object(menu, "get_settings_path", return_value=path),
                    patch.object(menu.updater, "check_for_updates", return_value=update, side_effect=error),
                    patch.object(menu.updater, "install_update") as install,
                ):
                    self.assertFalse(menu._settings_menu(lambda _: next(answers)))
                install.assert_not_called()

    def test_main_menu_only_enters_settings_and_does_not_check_updates(self):
        answers = Mock(side_effect=["3", "0"])
        with (
            patch.object(menu, "_settings_menu", return_value=False) as settings,
            patch.object(menu, "_check_updates") as check,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            menu.run_menu(input_fn=answers)
        settings.assert_called_once_with(answers)
        check.assert_not_called()
        self.assertIn("3. Настройки", output.getvalue())
        self.assertNotIn("Проверить обновления", output.getvalue())

    def test_settings_edits_parameter_and_preserves_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.ini"
            settings_path.write_text(
                "# keep\n[OBS Local paths]\nquality = standard\n",
                encoding="utf-8",
            )
            answers = iter(["1", "2k", "0"])
            with patch.object(menu.app, "BASE_DIR", root):
                menu._settings_menu(lambda _: next(answers))
            text = menu.get_settings_path(root).read_text(encoding="utf-8")
            self.assertIn("# keep", text)
            self.assertIn("quality = 2k", text)
            self.assertIn("quality = standard", settings_path.read_text(encoding="utf-8"))

    def test_settings_accepts_google_docs_program_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.ini").write_text(
                "[Song preparation]\nprogram_url = https://disk.yandex.ru/d/old\n",
                encoding="utf-8",
            )
            url = "https://docs.google.com/document/d/synthetic-program/edit?usp=sharing"
            answers = iter(["1", url, "0"])
            with patch.object(menu.app, "BASE_DIR", root):
                menu._settings_menu(lambda _: next(answers))
            settings = configparser.ConfigParser()
            settings.read(menu.get_settings_path(root), encoding="utf-8")
            self.assertEqual(settings.get("Song preparation", "program_url"), url)

    def test_malformed_legacy_url_does_not_trap_settings_or_updates(self):
        for answers, expected in (
            (["0"], "https://[broken"),
            (["1", "https://disk.yandex.ru/d/repaired", "0"], "https://disk.yandex.ru/d/repaired"),
            (["6", "0"], "https://[broken"),
        ):
            with self.subTest(answers=answers), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.ini"
                path.write_text("[Song preparation]\nprogram_url = https://[broken\n", encoding="utf-8")
                input_fn = Mock(side_effect=answers)
                with (
                    patch.object(menu, "get_settings_path", return_value=path),
                    patch.object(menu, "_check_updates", return_value=False) as check,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertFalse(menu._settings_menu(input_fn))
                self.assertEqual(input_fn.call_count, len(answers))
                self.assertEqual(check.call_count, int(answers[0] == "6"))
                settings = configparser.ConfigParser()
                settings.read(path, encoding="utf-8")
                self.assertEqual(settings.get("Song preparation", "program_url"), expected)

    def test_settings_exposes_priority_and_cloud_links_for_existing_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.ini"
            path.write_text(
                "# preserve\n[Song preparation]\nprogram_url = https://disk.yandex.ru/d/old\n",
                encoding="utf-8",
            )
            google_url = "https://docs.google.com/document/d/synthetic-program/edit?usp=sharing"
            answers = iter(["2", "1", "3", google_url, "2", "2", "0"])
            with (
                patch.object(menu, "get_settings_path", return_value=path),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                menu._settings_menu(lambda _: next(answers))
            settings = configparser.ConfigParser()
            settings.read(path, encoding="utf-8")
            self.assertEqual(settings.get("Song preparation", "priority_cloud_disk"), "yandex")
            self.assertEqual(settings.get("Song preparation", "program_google_url"), google_url)
            self.assertEqual(settings.get("Song preparation", "program_url"), "https://disk.yandex.ru/d/old")
            self.assertIn("1. Google", output.getvalue())
            self.assertIn("2. Яндекс", output.getvalue())
            self.assertIn("# preserve", path.read_text(encoding="utf-8"))

    def test_settings_rejects_invalid_priority_and_mismatched_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            original = (
                "[Song preparation]\npriority_cloud_disk = google\n"
                "program_google_url = https://docs.google.com/document/d/program/edit\n"
            )
            path.write_text(original, encoding="utf-8")
            answers = iter(["1", "bad", "2", "https://disk.yandex.ru/d/wrong-cloud", "0"])
            with (
                patch.object(menu, "get_settings_path", return_value=path),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                menu._settings_menu(lambda _: next(answers))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(output.getvalue().count("Не удалось изменить настройки"), 2)

    def test_settings_rejects_invalid_quality_and_keeps_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.ini"
            original = "[OBS Local paths]\nquality = standard\n"
            path.write_text(original, encoding="utf-8")
            answers = iter(["1", "bad", "0"])
            with patch.object(menu.app, "BASE_DIR", root):
                menu._settings_menu(lambda _: next(answers))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertIn(
                "quality = standard",
                menu.get_settings_path(root).read_text(encoding="utf-8"),
            )

    def test_unavailable_settings_keep_updates_reachable(self):
        for failure in ("missing", "malformed", "encoding", "unreadable", "migration"):
            for installed in (False, True):
                with (
                    self.subTest(failure=failure, installed=installed),
                    tempfile.TemporaryDirectory() as directory,
                    contextlib.ExitStack() as stack,
                ):
                    path = Path(directory) / "settings.ini"
                    if failure == "malformed":
                        path.write_text("[OBS Local paths]\nquality = standard\n[broken\n", encoding="utf-8")
                    elif failure == "encoding":
                        path.write_bytes(b"\xff")
                    elif failure == "unreadable":
                        stack.enter_context(patch.object(Path, "open", side_effect=PermissionError("denied")))
                    stack.enter_context(patch.object(
                        menu, "get_settings_path", return_value=path,
                        side_effect=PermissionError("migration denied") if failure == "migration" else None,
                    ))
                    check = stack.enter_context(patch.object(menu, "_check_updates", return_value=installed))
                    save = stack.enter_context(patch.object(menu, "update_ini_file"))
                    output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    input_fn = Mock(side_effect=["2"] if installed else ["2", "0"])

                    self.assertEqual(menu._settings_menu(input_fn), installed)

                    check.assert_called_once_with(input_fn)
                    save.assert_not_called()
                    self.assertEqual(input_fn.call_count, 1 if installed else 2)
                    self.assertIn("Не удалось прочитать настройки", output.getvalue())
                    self.assertIn("2. Проверить обновления", output.getvalue())
                    self.assertNotIn("quality = standard", output.getvalue())

    def test_missing_settings_allow_back_invalid_choice_and_eof(self):
        for answers in (["0"], ["invalid", "0"], [EOFError]):
            with (
                self.subTest(answers=answers),
                tempfile.TemporaryDirectory() as directory,
                patch.object(menu.app, "BASE_DIR", Path(directory)),
                patch.object(menu, "_check_updates") as check,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                input_fn = Mock(side_effect=answers)
                self.assertFalse(menu._settings_menu(input_fn))
                self.assertEqual(input_fn.call_count, len(answers))
                check.assert_not_called()

    def test_token_is_hidden_saved_privately_and_updates_local_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "obs_local"
            local.mkdir()
            (local / "settings.ini").write_text(
                "[OBS Local paths]\nquality = standard\n", encoding="utf-8"
            )
            (local / "config.ini").write_text(
                "[YandexDisk]\ntoken = old\n", encoding="utf-8"
            )
            answers = iter(["6", "0"])
            with (
                patch.object(menu.app, "BASE_DIR", local),
                patch.object(menu.app, "PROJECT_DIR", root),
                patch.object(menu.getpass, "getpass", return_value="synthetic-token"),
            ):
                menu._settings_menu(lambda _: next(answers))
            for path in (root / "config.ini", local / "config.ini"):
                parser = configparser.ConfigParser()
                parser.read(path)
                self.assertEqual(parser.get("YandexDisk", "token"), "synthetic-token")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_song_library_menu_does_not_read_program(self):
        answers = iter(["2", "0"])
        config = {"log_dir": Path("/dev/shm")}
        with (
            patch.object(menu.prepare_songs, "load_config", return_value=config),
            patch.object(menu.prepare_songs, "setup_logging"),
            patch.object(menu, "_update_song_library", new_callable=AsyncMock) as update,
            patch.object(menu, "_prepare_songs") as read_program,
        ):
            menu._songs_menu(lambda _: next(answers), debug=False)
        update.assert_awaited_once_with(config)
        read_program.assert_not_called()

    def test_library_update_calls_shared_cache_without_program(self):
        config = {
            "yandex_token": "synthetic-token",
            "yandex_zip_png_paths": ["/zip"],
            "yandex_pptx_paths": ["/pptx"],
        }
        with (
            patch.object(menu, "YandexDiskClient") as client,
            patch.object(menu.prepare_songs, "_cache_remote_song_zips",
                         new_callable=AsyncMock, return_value=(2, 0)) as cache,
            patch.object(menu.prepare_songs, "download_program_docx") as download,
        ):
            asyncio.run(menu._update_song_library(config))
        self.assertEqual(cache.await_args.args[:2], (client.return_value, config))
        download.assert_not_called()

    def test_library_update_reports_partial_failures(self):
        config = {
            "yandex_token": "synthetic-token",
            "yandex_zip_png_paths": ["/zip"],
            "yandex_pptx_paths": [],
        }
        with (
            patch.object(menu, "YandexDiskClient"),
            patch.object(menu.prepare_songs, "_cache_remote_song_zips",
                         new_callable=AsyncMock, return_value=(1, 1)),
        ):
            with self.assertRaisesRegex(RuntimeError, "Некоторые материалы"):
                asyncio.run(menu._update_song_library(config))

    def test_library_update_requires_token(self):
        with self.assertRaisesRegex(RuntimeError, "токен"):
            asyncio.run(menu._update_song_library({"yandex_token": ""}))

    def test_song_cancellation_returns_to_submenu_without_error(self):
        answers = iter(["1", "0"])
        with (
            patch.object(menu.prepare_songs, "load_config",
                         return_value={"log_dir": Path("/synthetic/logs")}),
            patch.object(menu.prepare_songs, "setup_logging"),
            patch.object(menu.prepare_songs, "run", new_callable=AsyncMock,
                         side_effect=menu.prepare_songs.OperationCancelled),
            patch.object(menu.prepare_songs, "log_user_error") as log_error,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            menu._songs_menu(lambda _: next(answers), debug=False)
        self.assertEqual(output.getvalue().count("1. Прочитать программу"), 2)
        self.assertIn("Подготовка песен отменена.", output.getvalue())
        self.assertNotIn("Ошибка подготовки", output.getvalue())
        log_error.assert_not_called()

    def test_direct_song_cli_cancellation_is_not_an_error(self):
        with (
            patch("sys.argv", ["prepare_songs"]),
            patch.object(menu.prepare_songs, "load_config",
                         return_value={"log_dir": Path("/synthetic/logs")}),
            patch.object(menu.prepare_songs, "setup_logging"),
            patch.object(menu.prepare_songs, "run", new_callable=AsyncMock,
                         side_effect=menu.prepare_songs.OperationCancelled),
            patch.object(menu.prepare_songs, "log_user_error") as log_error,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            menu.prepare_songs.main()
        self.assertIn("Подготовка песен отменена.", output.getvalue())
        log_error.assert_not_called()

if __name__ == "__main__":
    unittest.main()
