import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from bot import folder_setup
from bot.folder_setup import (
    ensure_sunday_folder_tree,
    seconds_until_next_monday,
    should_check_folder_tree,
)


class FakeYandexDiskClient:
    def __init__(self, folders=None):
        self.folders = set(folders or ())
        self.ensure_calls = []

    async def folder_exists(self, path):
        return path in self.folders

    async def find_child_folder(self, parent_path, predicate):
        for path in sorted(self.folders):
            parent, _, name = path.rpartition("/")
            if parent == parent_path and predicate(name):
                return {"name": name, "path": path}
        return None

    async def ensure_folder(self, path):
        self.ensure_calls.append(path)
        parts = [part for part in path.split("/") if part]
        current = ""
        for part in parts:
            current += f"/{part}"
            self.folders.add(current)
        return True


class FolderTreeCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_month_date_and_template_tree(self):
        template = Path(__file__).resolve().parents[1] / "template.yaml"
        client = FakeYandexDiskClient({"/presentations"})

        sunday_path = await ensure_sunday_folder_tree(
            client,
            "/presentations",
            datetime(2026, 11, 8),
            template,
        )

        self.assertEqual(
            sunday_path, "/presentations/11 НОЯБРЬ 2026/08.11.2026"
        )
        self.assertIn(f"{sunday_path}/Служение", client.folders)
        self.assertIn(
            f"{sunday_path}/Трансляция/Гимны - png/1 -", client.folders
        )
        self.assertIn(
            f"{sunday_path}/Трансляция/Молитва/Молитва слайды - png",
            client.folders,
        )
        self.assertIn(f"{sunday_path}/Трансляция/pptx2png", client.folders)

    async def test_reuses_existing_date_folder_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "template.yaml"
            template.write_text("structure:\n  Служение: {}\n", encoding="utf-8")
            month_path = "/presentations/11 ноябрь 2026"
            existing_date = f"{month_path}/2026-11-08"
            client = FakeYandexDiskClient(
                {"/presentations", month_path, existing_date}
            )

            sunday_path = await ensure_sunday_folder_tree(
                client,
                "/presentations",
                datetime(2026, 11, 8),
                template,
            )

        self.assertEqual(sunday_path, existing_date)
        self.assertNotIn(f"{month_path}/08.11.2026", client.folders)
        self.assertIn(f"{existing_date}/Служение", client.folders)

    async def test_does_not_create_base_path_when_it_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "template.yaml"
            template.write_text("structure:\n  Служение: {}\n", encoding="utf-8")
            client = FakeYandexDiskClient()

            with self.assertRaisesRegex(RuntimeError, "Базовая папка"):
                await ensure_sunday_folder_tree(
                    client,
                    "/typo",
                    datetime(2026, 11, 8),
                    template,
                )

        self.assertEqual(client.ensure_calls, [])


class FolderSchedulePolicyTests(unittest.TestCase):
    def test_runs_on_startup_for_missed_monday_catch_up(self):
        self.assertTrue(
            should_check_folder_tree(
                datetime(2026, 11, 3, 10),
                None,
                startup=True,
            )
        )

    def test_runs_on_monday_until_target_sunday_succeeds(self):
        monday = datetime(2026, 11, 2, 10)
        self.assertTrue(should_check_folder_tree(monday, None, startup=False))
        self.assertFalse(
            should_check_folder_tree(
                monday,
                date(2026, 11, 8),
                startup=False,
            )
        )

    def test_retries_failed_setup_after_monday(self):
        self.assertTrue(
            should_check_folder_tree(
                datetime(2026, 11, 3, 10),
                None,
                startup=False,
                retry_pending=True,
            )
        )

    def test_does_not_run_on_other_days_after_startup(self):
        self.assertFalse(
            should_check_folder_tree(
                datetime(2026, 11, 3, 10),
                None,
                startup=False,
            )
        )

    def test_next_check_is_monday_midnight_in_local_time(self):
        monday = datetime(2026, 11, 2, 0)
        tuesday = datetime(2026, 11, 3, 10)

        self.assertEqual(seconds_until_next_monday(monday), 7 * 24 * 60 * 60)
        self.assertEqual(
            seconds_until_next_monday(tuesday),
            6 * 24 * 60 * 60 - 10 * 60 * 60,
        )


class FolderSetupLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_failure_and_then_sleeps_until_next_monday(self):
        now = datetime(2026, 11, 3, 10)
        with (
            patch.object(folder_setup, "datetime") as datetime_mock,
            patch.object(
                folder_setup,
                "ensure_sunday_folder_tree",
                new_callable=AsyncMock,
            ) as ensure_tree,
            patch.object(
                folder_setup.asyncio,
                "sleep",
                new_callable=AsyncMock,
            ) as sleep,
        ):
            datetime_mock.now.side_effect = [now, now]
            ensure_tree.side_effect = [RuntimeError("temporary failure"), "/target"]
            sleep.side_effect = [None, asyncio.CancelledError]

            with self.assertLogs(level="ERROR"):
                with self.assertRaises(asyncio.CancelledError):
                    await folder_setup.folder_setup_loop(
                        FakeYandexDiskClient(),
                        "/presentations",
                        Path("template.yaml"),
                        interval_sec=600,
                    )

        self.assertEqual(ensure_tree.await_count, 2)
        sleep.assert_has_awaits(
            [
                call(600),
                call(seconds_until_next_monday(now)),
            ]
        )


if __name__ == "__main__":
    unittest.main()
