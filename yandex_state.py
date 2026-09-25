# ==========================================
# yandex_state.py — ГЛОБАЛЬНОЕ СОСТОЯНИЕ ЯНДЕКС.ДИСКА (v1.1)
# ==========================================

import asyncio
import secrets
from typing import Optional, Dict, Set

from yandex_disk import YandexDiskClient


# ==========================================
# КОНФИГ (устанавливается из bot.py)
# ==========================================

class YandexConfig:
    """Конфигурация Yandex-подсистемы. Устанавливается из bot.py при старте."""

    def __init__(self):
        self.client: Optional[YandexDiskClient] = None
        self.base_path: str = ""
        self.source_folder: str = "Служение"
        self.target_folder: str = "Трансляция"
        self.pptx2png_folder: str = "pptx2png"
        self.sermon_folder: str = "проповедь - png"
        self.sermon_keyword: str = "проповед"
        self.template_file: str = "template.yaml"

        # ✅ Таймаут ожидания ответа на промпт (секунды)
        self.prompt_timeout_sec: int = 1800

        # ✅ Категории слайдов (этап 2)
        # Списки ключевых слов. Используются sermon_detector.
        self.sermon_keywords: list = ["проповед", "проповедь"]
        self.opening_keywords: list = ["начало", "в начале"]
        self.prayer_keywords: list = ["молитва", "молиться"]


config = YandexConfig()


# ==========================================
# ОБЩИЕ СЕССИИ (и обычные, и Yandex)
# ==========================================

sessions: Dict[str, dict] = {}


# ==========================================
# БЛОКИРОВКИ YANDEX-СЕССИЙ
# ==========================================

yd_session_lock = asyncio.Lock()

# {user_id:chat_id -> nonce (generation)}
yd_active_sessions: Dict[str, str] = {}

# Реестр активных Yandex-задач (защита от cleaner)
yd_active_tasks: Set[str] = set()


# ==========================================
# НИЗКОУРОВНЕВЫЕ ПРИМИТИВЫ БЛОКИРОВОК
# ==========================================

def yd_session_key(user_id: int, chat_id: int) -> str:
    return f"{user_id}:{chat_id}"


async def yd_try_acquire(user_id: int, chat_id: int) -> Optional[str]:
    """
    Захватывает блокировку для сессии.
    Возвращает nonce (generation) при успехе или None, если уже занято.
    """
    key = yd_session_key(user_id, chat_id)
    nonce = secrets.token_hex(8)
    async with yd_session_lock:
        if key in yd_active_sessions:
            return None
        yd_active_sessions[key] = nonce
        return nonce


async def yd_release(user_id: int, chat_id: int, nonce: Optional[str] = None) -> bool:
    """
    Освобождает блокировку.
    Если передан nonce — снимает только при совпадении.
    """
    key = yd_session_key(user_id, chat_id)
    async with yd_session_lock:
        current = yd_active_sessions.get(key)
        if current is None:
            return True
        if nonce is not None and current != nonce:
            return False
        yd_active_sessions.pop(key, None)
        return True


async def yd_is_active(user_id: int, chat_id: int, nonce: Optional[str] = None) -> bool:
    """
    Проверяет, активна ли сессия.
    Если передан nonce — проверяет и совпадение.
    """
    key = yd_session_key(user_id, chat_id)
    async with yd_session_lock:
        current = yd_active_sessions.get(key)
        if current is None:
            return False
        if nonce is not None and current != nonce:
            return False
        return True