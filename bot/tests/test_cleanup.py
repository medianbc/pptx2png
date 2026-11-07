import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot import bot as bot_module
from bot import yandex_state


class CleanupOldTasksTests(unittest.IsolatedAsyncioTestCase):
    async def test_removes_stale_uploads_but_preserves_active_uploads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shm_dir = Path(temp_dir)
            stale_upload = shm_dir / "yd_upload_stale"
            active_upload = shm_dir / "yd_upload_active"
            stale_upload.mkdir()
            active_upload.mkdir()
            os.utime(stale_upload, (1, 1))
            os.utime(active_upload, (1, 1))
            yandex_state.yd_active_uploads.add(active_upload.name)

            try:
                with patch.object(
                    bot_module.task_lock_manager,
                    "is_active",
                    new_callable=AsyncMock,
                    return_value=False,
                ):
                    await bot_module.cleanup_old_tasks_async(
                        shm_dir, max_age_seconds=0
                    )

                self.assertFalse(stale_upload.exists())
                self.assertTrue(active_upload.is_dir())
            finally:
                yandex_state.yd_active_uploads.discard(active_upload.name)


if __name__ == "__main__":
    unittest.main()
