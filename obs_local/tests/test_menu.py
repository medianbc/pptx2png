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
        prepare_songs.assert_called_once_with()

    def test_eof_exits_cleanly(self):
        menu.run_menu(input_fn=Mock(side_effect=EOFError))

    def test_date_argument_keeps_direct_presentation_launch(self):
        with (
            patch("sys.argv", ["obs_local", "--date", "2026-11-08"]),
            patch.object(menu.app, "main") as app_main,
            patch.object(menu, "run_menu") as run_menu,
        ):
            menu.main()

        app_main.assert_called_once_with()
        run_menu.assert_not_called()


if __name__ == "__main__":
    unittest.main()
