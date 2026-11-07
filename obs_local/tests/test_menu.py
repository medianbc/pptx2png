import unittest
from unittest.mock import Mock, patch

from obs_local import menu


class MainMenuTests(unittest.TestCase):
    def test_routes_to_each_workflow_and_exits(self):
        answers = iter(["invalid", "1", "2", "0"])

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
        answers = iter(["1", "2", "0"])
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
        answers = iter(["3", "y"])
        update = menu.updater.UpdateInfo(
            branch="main",
            local_revision="a" * 40,
            remote_revision="b" * 40,
            available=True,
            is_git_checkout=False,
        )
        with (
            patch.object(menu.updater, "check_for_updates", return_value=update),
            patch.object(menu.updater, "install_update") as install_update,
        ):
            menu.run_menu(input_fn=lambda _: next(answers))

        install_update.assert_called_once_with(update)

    def test_update_check_can_be_cancelled(self):
        answers = iter(["3", "n", "0"])
        update = menu.updater.UpdateInfo(
            branch="main",
            local_revision="a" * 40,
            remote_revision="b" * 40,
            available=True,
            is_git_checkout=False,
        )
        with (
            patch.object(menu.updater, "check_for_updates", return_value=update),
            patch.object(menu.updater, "install_update") as install_update,
        ):
            menu.run_menu(input_fn=lambda _: next(answers))

        install_update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
