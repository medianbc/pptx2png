import asyncio
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import yandex_flow_handlers as flow_handlers
from bot import yandex_flow_core
from bot import yandex_state
from pptx import Presentation


class SundayUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user_id = 123
        self.chat_id = 456
        self.session_key = f"yd_{self.user_id}_{self.chat_id}"
        self.session = {
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "sunday": date(2026, 11, 8),
            "paths": {"source": "/source", "target": "/target"},
            "files": [],
            "nonce": "testnonce",
            "task_ids": [],
            "processing": False,
            "awaiting_upload": True,
            "cancelled": False,
        }
        yandex_state.sessions[self.session_key] = self.session

    def tearDown(self):
        yandex_state.sessions.pop(self.session_key, None)

    async def test_no_files_starts_upload_waiting_session(self):
        status_message = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.user_id),
            chat=SimpleNamespace(id=self.chat_id),
            reply=AsyncMock(return_value=status_message),
        )
        client = SimpleNamespace(check_access=AsyncMock(return_value=(True, None)))
        old_client = yandex_state.config.client
        old_base_path = yandex_state.config.base_path
        yandex_state.config.client = client
        yandex_state.config.base_path = "/presentations"
        yandex_state.sessions.pop(self.session_key, None)

        try:
            with (
                patch.object(
                    flow_handlers, "get_nearest_sunday",
                    return_value=date(2026, 11, 8),
                ),
                patch.object(
                    flow_handlers, "month_folder_name",
                    return_value="НОЯБРЬ 2026",
                ),
                patch.object(
                    flow_handlers, "resolve_sunday_paths",
                    new_callable=AsyncMock,
                    return_value={"source": "/source", "target": "/target"},
                ),
                patch.object(
                    flow_handlers, "find_pptx_in_source",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
            ):
                await flow_handlers.cmd_sunday(
                    message, AsyncMock(return_value=True), SimpleNamespace()
                )
        finally:
            yandex_state.config.client = old_client
            yandex_state.config.base_path = old_base_path

        stored = yandex_state.sessions[self.session_key]
        self.assertTrue(stored["awaiting_upload"])
        self.assertFalse(stored["processing"])
        self.assertIn("Отправьте сюда файл", status_message.edit_text.await_args.args[0])

    async def test_uploaded_pptx_is_sent_through_sunday_pipeline(self):
        status_message = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.user_id),
            chat=SimpleNamespace(id=self.chat_id),
            message_id=1,
            document=SimpleNamespace(
                file_name="Презентация.pptx",
                file_id="telegram-file-id",
            ),
            reply=AsyncMock(return_value=status_message),
        )
        bot = SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(file_path="remote-file")),
            download_file=AsyncMock(),
        )

        async def download_file(_file_path, destination):
            Path(destination).write_bytes(b"pptx-content")

        bot.download_file.side_effect = download_file
        with tempfile.TemporaryDirectory() as shm_dir:
            with patch.object(
                flow_handlers, "_yd_prepare_files", new_callable=AsyncMock
            ) as prepare_files:
                await flow_handlers.handle_sunday_upload(
                    message,
                    bot,
                    shm_dir,
                    SimpleNamespace(),
                    AsyncMock(return_value=True),
                )

            prepare_files.assert_awaited_once()
            kwargs = prepare_files.await_args.kwargs
            self.assertIsNone(kwargs["callback"])
            self.assertEqual(kwargs["session_key"], self.session_key)
            self.assertEqual(kwargs["nonce"], "testnonce")
            self.assertEqual(kwargs["owner_user_id"], self.user_id)
            file_item = kwargs["files_to_process"][0]
            self.assertEqual(file_item["name"], "Презентация.pptx")
            self.assertEqual(Path(file_item["local_path"]).read_bytes(), b"pptx-content")
            self.assertTrue(self.session["processing"])
            self.assertFalse(self.session["awaiting_upload"])
            Path(file_item["local_path"]).unlink()
            Path(file_item["local_path"]).parent.rmdir()

    async def test_local_upload_is_prepared_without_yandex_download(self):
        with tempfile.TemporaryDirectory() as shm_dir:
            source_dir = Path(shm_dir) / "yd_upload_unit_test"
            source_dir.mkdir()
            source = source_dir / "uploaded.pptx"
            presentation = Presentation()
            presentation.slides.add_slide(presentation.slide_layouts[6])
            presentation.save(source)

            status_message = SimpleNamespace(message_id=55)
            picker = {
                "user_id": self.user_id,
                "chat_id": self.chat_id,
                "nonce": "testnonce",
                "task_ids": [],
                "cancelled": False,
            }
            yandex_state.sessions[self.session_key] = picker
            client = SimpleNamespace(download_file=AsyncMock())
            old_client = yandex_state.config.client
            yandex_state.config.client = client

            async def run_thread(_task_id, fn, *args, **kwargs):
                return await asyncio.to_thread(fn, *args, **kwargs)

            async def pass_spinner(_status, _task_id, _text, coro):
                return await coro

            try:
                with (
                    patch.object(
                        yandex_flow_core, "_yd_to_thread", side_effect=run_thread
                    ),
                    patch.object(
                        yandex_flow_core, "_yd_with_spinner",
                        side_effect=pass_spinner,
                    ),
                    patch.object(
                        yandex_flow_core, "_safe_edit", new_callable=AsyncMock
                    ),
                    patch.object(
                        yandex_flow_core, "extract_speaker_notes",
                        return_value=(True, [], False),
                    ),
                    patch.object(
                        yandex_flow_core, "_yd_render_category_prompt",
                        new_callable=AsyncMock,
                    ),
                ):
                    await yandex_flow_core._yd_prepare_files_impl(
                        callback=None,
                        bot=SimpleNamespace(),
                        SHM_DIR=shm_dir,
                        user_mgr=SimpleNamespace(
                            get_user_config=lambda _user_id: {}
                        ),
                        files_to_process=[{
                            "name": source.name,
                            "size": source.stat().st_size,
                            "local_path": str(source),
                        }],
                        sunday=self.session["sunday"],
                        paths=self.session["paths"],
                        session_key=self.session_key,
                        nonce="testnonce",
                        status_msg=status_message,
                        owner_user_id=self.user_id,
                        chat_id=self.chat_id,
                    )

                client.download_file.assert_not_awaited()
                task_id = picker["task_ids"][0]
                prepared = yandex_state.sessions[task_id]["pending"]["prepared"][0]
                self.assertEqual(prepared["total_slides"], 1)
                self.assertTrue(Path(prepared["file_path"]).is_file())
            finally:
                yandex_state.config.client = old_client
                task_id = picker["task_ids"][0] if picker["task_ids"] else None
                if task_id is not None:
                    task_session = yandex_state.sessions.pop(task_id, {})
                    yandex_state.yd_active_tasks.discard(task_id)
                    task_dir = task_session.get("pending", {}).get("task_dir")
                    if task_dir:
                        shutil.rmtree(task_dir, ignore_errors=True)
                yandex_state.sessions.pop(self.session_key, None)

    async def test_unsupported_upload_keeps_session_waiting(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.user_id),
            chat=SimpleNamespace(id=self.chat_id),
            document=SimpleNamespace(file_name="notes.pdf", file_id="file-id"),
            reply=AsyncMock(),
        )
        await flow_handlers.handle_sunday_upload(
            message,
            SimpleNamespace(),
            "/dev/shm",
            SimpleNamespace(),
            AsyncMock(return_value=True),
        )

        message.reply.assert_awaited_once()
        self.assertTrue(self.session["awaiting_upload"])
        self.assertFalse(self.session["processing"])

    def test_upload_filter_matches_only_own_active_session(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.user_id),
            chat=SimpleNamespace(id=self.chat_id),
        )
        self.assertTrue(flow_handlers._is_awaiting_sunday_upload(message))

        message.from_user.id = self.user_id + 1
        self.assertFalse(flow_handlers._is_awaiting_sunday_upload(message))


if __name__ == "__main__":
    unittest.main()
