import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot import handlers, yandex_state


class AdminFolderSetupHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_cannot_confirm_folder_creation(self):
        callback = type(
            "Callback",
            (),
            {
                "from_user": type("User", (), {"id": 20})(),
                "answer": AsyncMock(),
            },
        )()

        with patch.object(
            handlers, "ensure_sunday_folder_tree", new_callable=AsyncMock
        ) as create_tree:
            await handlers.handle_admin_confirm_sunday_tree(
                callback, ADMIN_ID=10, YANDEX_TEMPLATE_PATH=Path("template.yaml")
            )

        callback.answer.assert_awaited_once_with(
            "❌ Доступ запрещен.", show_alert=True
        )
        create_tree.assert_not_awaited()

    async def test_admin_confirmation_creates_upcoming_sunday_tree(self):
        status_message = type("Message", (), {"edit_text": AsyncMock()})()
        message = type(
            "Message",
            (),
            {"answer": AsyncMock(return_value=status_message)},
        )()
        callback = type(
            "Callback",
            (),
            {
                "from_user": type("User", (), {"id": 10})(),
                "data": "admin_confirm_sunday_tree:2026-11-08",
                "message": message,
                "answer": AsyncMock(),
            },
        )()
        original_client = yandex_state.config.client
        original_base_path = yandex_state.config.base_path
        yandex_state.config.client = object()
        yandex_state.config.base_path = "/presentations"
        try:
            with (
                patch.object(
                    handlers,
                    "get_nearest_sunday",
                    return_value=datetime(2026, 11, 8, 10),
                ),
                patch.object(
                    handlers,
                    "ensure_sunday_folder_tree",
                    new_callable=AsyncMock,
                    return_value="/presentations/11 НОЯБРЬ 2026/08.11.2026",
                ) as create_tree,
            ):
                await handlers.handle_admin_confirm_sunday_tree(
                    callback,
                    ADMIN_ID=10,
                    YANDEX_TEMPLATE_PATH=Path("template.yaml"),
                )
        finally:
            yandex_state.config.client = original_client
            yandex_state.config.base_path = original_base_path

        create_tree.assert_awaited_once()
        args = create_tree.await_args.args
        self.assertEqual(args[1], "/presentations")
        self.assertEqual(args[2].date(), date(2026, 11, 8))
        self.assertEqual(args[3], Path("template.yaml"))
        status_message.edit_text.assert_awaited_once()
        self.assertIn("✅ Структура папок", status_message.edit_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
