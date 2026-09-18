# ==========================================
# handlers.py — ОБРАБОТЧИКИ (v1.1)
# ==========================================

import os
import shutil
import logging
import secrets
import asyncio
import zipfile
import time
from pathlib import Path
from typing import Optional, Set, Dict, List, Tuple
from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils import (
    extract_text_from_pptx,
    check_spelling,
    download_file_by_url,
    download_yandex_disk,
    core_pipeline,
)
import converter_engine
from converter_engine import make_dark_mode

from yandex_disk import (
    YandexDiskClient,
    YandexDiskError,
    YandexDiskNotFoundError,
    get_nearest_sunday,
    month_folder_name,
    resolve_sunday_paths,
    find_pptx_in_source,
    pptx_matches_date,
)


# ==========================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ (Яндекс.Диск)
# ==========================================

yandex_client: Optional[YandexDiskClient] = None
yandex_base_path: str = ""
yandex_source_folder: str = "Служение"
yandex_target_folder: str = "Трансляция"
yandex_pptx2png_folder: str = "pptx2png"
yandex_sermon_folder: str = "проповедь - png"
yandex_sermon_keyword: str = "проповед"
yandex_template_file: str = "template.yaml"


# ==========================================
# БЛОКИРОВКА СЕССИЙ ЯНДЕКС.ДИСКА
# ==========================================

yd_session_lock = asyncio.Lock()
yd_active_sessions: Set[str] = set()


def yd_session_key(user_id: int, chat_id: int) -> str:
    return f"{user_id}:{chat_id}"


async def yd_try_acquire(user_id: int, chat_id: int) -> bool:
    """Атомарно проверяет и добавляет ключ сессии."""
    key = yd_session_key(user_id, chat_id)
    async with yd_session_lock:
        if key in yd_active_sessions:
            return False
        yd_active_sessions.add(key)
        return True


async def yd_release(user_id: int, chat_id: int):
    """Убирает ключ сессии."""
    key = yd_session_key(user_id, chat_id)
    async with yd_session_lock:
        yd_active_sessions.discard(key)


# ==========================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ (общие)
# ==========================================

sessions: Dict[str, dict] = {}
router = Router()
converter_semaphore = asyncio.Semaphore(2)


# ==========================================
# МЕНЕДЖЕР БЛОКИРОВОК ЗАДАЧ
# ==========================================

class TaskLockManager:
    """Менеджер блокировок для защиты от дублирующих операций."""

    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._active: Set[str] = set()
        self._dict_lock = asyncio.Lock()

    async def acquire(self, task_id: str) -> bool:
        async with self._dict_lock:
            if task_id in self._active:
                return False
            if task_id not in self._locks:
                self._locks[task_id] = asyncio.Lock()
            lock = self._locks[task_id]
            if lock.locked():
                return False
            await lock.acquire()
            self._active.add(task_id)
            return True

    async def release(self, task_id: str):
        async with self._dict_lock:
            self._active.discard(task_id)
            if task_id in self._locks:
                lock = self._locks[task_id]
                if lock.locked():
                    lock.release()
                self._locks.pop(task_id, None)

    async def is_active(self, task_id: str) -> bool:
        async with self._dict_lock:
            return task_id in self._active


task_lock_manager = TaskLockManager()


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def safe_filename(filename: str) -> str:
    import re
    safe_name = os.path.basename(filename)
    safe_name = re.sub(r'[^\w\s.-]', '', safe_name)
    safe_name = re.sub(r'\s+', ' ', safe_name).strip()
    if not safe_name:
        safe_name = f"file_{secrets.token_hex(4)}"
    if len(safe_name) > 100:
        name, ext = os.path.splitext(safe_name)
        safe_name = name[:90] + ext
    return safe_name


def validate_download_path(task_dir: Path, destination: Path) -> bool:
    try:
        return destination.resolve().parent == task_dir.resolve() or \
               destination.resolve().parent in task_dir.resolve().parents
    except Exception:
        return False


def generate_task_id(chat_id: int, user_id: int, message_id: int) -> str:
    return f"task_{chat_id}_{user_id}_{message_id}_{secrets.token_hex(8)}"


def parse_slides_ranges(input_text: str) -> List[Tuple[int, int]]:
    ranges = []
    parts = input_text.replace(" ", "").split(",")
    for part in parts:
        if not part:
            continue
        if "-" in part:
            try:
                start, end = map(int, part.split("-"))
                if start < 1 or end < 1:
                    return []
                if start > end:
                    start, end = end, start
                ranges.append((start, end))
            except ValueError:
                return []
        else:
            try:
                num = int(part)
                if num < 1:
                    return []
                ranges.append((num, num))
            except ValueError:
                return []
    return ranges


def reset_awaiting_for_user_chat(user_id: int, chat_id: int, exclude_task_id: Optional[str] = None):
    for tid, sess in sessions.items():
        if sess.get("user_id") == user_id and sess.get("chat_id") == chat_id:
            if exclude_task_id is None or tid != exclude_task_id:
                sess["awaiting_selection"] = False


def get_disabled_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏳ Конвертация...", callback_data="disabled_placeholder"))
    return kb


def touch_task(task_dir: Path):
    if task_dir and
