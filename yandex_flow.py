# ==========================================
# yandex_flow.py — ОРКЕСТРАЦИЯ ЯНДЕКС.ДИСКА (v4.0)
# ==========================================
# Изменения v4.0 (категории):
#   • 4 категории: opening > prayer > sermon (+ "остальные").
#     Порядок = приоритет при пересечении, задан в yandex_state.
#   • item["categories"] — новая структура: {cat: {ranges, matches,
#     found, manual}}.
#   • item["selected_categories"] — set из выбранных пользователем
#     в промпте категорий (opening, prayer, sermon, other).
#   • Новая клавиатура промпта с toggle-кнопками (yd_cat_toggle:*)
#     и кнопкой запуска (yd_cat_convert:*).
#   • Дефолтные галки — из user_config (process_opening/prayer/
#     sermon/other).
#   • Конвертация N+1 ZIP: по одному на выбранную категорию + один
#     на "остальные". Каждая категория — в свою папку первого
#     уровня под target_base.
#   • Старые yd_sermon_mode / yd_sermon_edit / _yd_render_sermon_prompt
#     сохранены для обратной совместимости (fallback, используется
#     handlers.py для ручного ввода диапазона проповеди).
#   • _is_sermon_slide оставлена как обёртка для совместимости.
# ==========================================
# Изменения v3.11 (по логам v3.10):
#   • _run_worker_detached — общий запуск worker'а в фоне.
#   • _yd_prepare_files / _yd_convert_and_upload — detached task.
#   • CancelledError не долетает до aiogram.
#   • cmd_sunday: draft-сессия под локом.
#   • _picker_is_active учитывает _draft-сессию.
#   • cmd_cancel_yd / yd_cancel_callback отменяют detached worker'ы.
# ==========================================
# Изменения v3.10 (по логам v3.9):
#   • _safe_answer — глотает «query is too old».
#   • callback.answer вызывается СРАЗУ в начале хендлера.
#   • _cancel_worker_async — фоновая отмена worker'а.
# ==========================================
# Изменения v3.9 (третий раунд аудита):
#   • Watchdog: атомарная перепроверка prompt_nonce под локом.
#   • _normalize_item_ranges: единый источник правды ranges vs start/end.
#   • TelegramBadRequest вместо строкового матчинга.
#   • _yd_prepare_files: полный rollback регистрации при ошибке mkdir.
# ==========================================
# Изменения v3.8 (второй раунд аудита):
#   • _cleaning_tasks — идемпотентность _yd_cleanup_task
#   • _deferred_dirs — защита от двойного deferred cleanup
#   • _yd_async_protected — защита сетевых корутин Яндекса
#   • /cancel_yd отменяет worker'ов
# ==========================================
# Изменения v3.7 (фиксы Qodo):
#   • Bug #1: cancel не освобождает picker при живых sibling'ах
#   • Bug #2: worker_task регистрируется сразу в _yd_prepare_files
#   • Bug #3: wait_for(shield(worker)) в cancel-хендлере
# ==========================================

import asyncio
import html as html_module
import logging
import os
import secrets
import shutil
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Callable, Awaitable, List, Dict, Tuple

from aiogram import Router, F, types, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import yandex_state
from yandex_state import (
    sessions,
    yd_session_lock,
    yd_active_tasks,
    yd_try_acquire,
    yd_release,
    yd_is_active,
)

from yandex_disk import (
    YandexDiskError,
    get_nearest_sunday,
    month_folder_name,
    resolve_sunday_paths,
    find_pptx_in_source,
)
from structure import safe_folder_name
from sermon_detector import find_sermon_range
from utils import extract_speaker_notes
from converter_engine import (
    convert_all_pngs,
    create_zip_stream,
    ppt_to_pptx_crossplatform,
    librenormalize_to_pptx,
)


router = Router()


# ==========================================
# МЕТАДАННЫЕ КАТЕГОРИЙ
# ==========================================

# Порядок из yandex_state.config (порядок = приоритет при пересечении).
# Если config ещё не инициализирован (edge-case), используем дефолт.
_DEFAULT_CATEGORY_ORDER = ["opening", "prayer", "sermon"]

_DEFAULT_CATEGORY_META = {
    "opening": {
        "name": "Начало",
        "emoji": "🌅",
        "folder_attr": "opening_folder",
        "zip_suffix": "начало",
        "settings_flag": "process_opening",
    },
    "prayer": {
        "name": "Молитва",
        "emoji": "🙏",
        "folder_attr": "prayer_folder",
        "zip_suffix": "молитва",
        "settings_flag": "process_prayer",
    },
    "sermon": {
        "name": "Проповедь",
        "emoji": "🎯",
        "folder_attr": "sermon_folder",
        "zip_suffix": "проповедь",
        "settings_flag": "process_sermon",
    },
}


def _cat_order() -> List[str]:
    """Возвращает актуальный порядок категорий."""
    return getattr(
        yandex_state.config, "category_order", _DEFAULT_CATEGORY_ORDER
    ) or _DEFAULT_CATEGORY_ORDER


def _cat_meta() -> Dict[str, dict]:
    """Возвращает актуальные метаданные категорий."""
    return getattr(
        yandex_state.config, "category_meta", _DEFAULT_CATEGORY_META
    ) or _DEFAULT_CATEGORY_META


def _cat_folder_path(cat: str, target_base: str) -> str:
    """Возвращает полный путь папки категории под target_base."""
    meta = _cat_meta().get(cat, {})
    folder_attr = meta.get("folder_attr")
    folder_name = (
        getattr(yandex_state.config, folder_attr, "")
        if folder_attr else ""
    )
    if not folder_name:
        # Fallback — имя из метаданных
        folder_name = meta.get("name", cat)
    return f"{target_base}/{folder_name}"


# ==========================================
# УТИЛИТЫ
# ==========================================

def _touch_task(task_dir: Path):
    """Обновляет mtime папки — чтобы cleaner не удалял активную задачу."""
    if task_dir and task_dir.exists():
        try:
            os.utime(task_dir, None)
        except Exception as e:
            logging.error(f"Ошибка touch для {task_dir}: {e}")


def _safe_delete_task_dir(task_dir: Path):
    """Безопасно удаляет папку задачи."""
    if task_dir and task_dir.exists():
        try:
            shutil.rmtree(task_dir)
            logging.info(f"🧹 Удалена папка задачи: {task_dir}")
        except Exception as e:
            logging.error(f"Ошибка удаления папки {task_dir}: {e}")


def _safe_unlink(path: Path):
    """Безопасно удаляет файл, логирует ошибку при неудаче."""
    if path is None:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        logging.warning(f"Не удалось удалить {path}: {e}")


async def _safe_edit(msg, text: str, **kwargs) -> bool:
    """Безопасный edit_text. Различает служебные TelegramBadRequest."""
    if msg is None:
        return False
    try:
        await msg.edit_text(text, **kwargs)
        return True
    except TelegramBadRequest as e:
        s = str(e)
        if "message is not modified" in s:
            return False
        if "query is too old" in s or "query ID is invalid" in s:
            logging.debug(f"[YD-EDIT] query устарел: {e}")
            return False
        logging.warning(f"TelegramBadRequest в edit_text: {e}")
        return False
    except Exception as e:
        logging.warning(
            f"Не удалось обновить статус-сообщение: {e}", exc_info=True
        )
        return False


async def _safe_answer(
    callback: types.CallbackQuery,
    text: str = "",
    show_alert: bool = False,
) -> None:
    """Безопасный callback.answer — глотает 'query is too old'."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as e:
        s = str(e)
        if "query is too old" in s or "query ID is invalid" in s:
            logging.info(f"[YD-ANSWER] callback устарел: {e}")
        else:
            logging.warning(f"[YD-ANSWER] TelegramBadRequest: {e}")
    except Exception as e:
        logging.warning(f"[YD-ANSWER] не удалось ответить на callback: {e}")


def _format_ranges_text(ranges, start=None, end=None) -> str:
    """Форматирует список диапазонов в '1–2, 8–9'."""
    if ranges:
        return ", ".join(
            f"{s}–{e}" if s != e else str(s) for s, e in ranges
        )
    if start is not None and end is not None:
        if start != end:
            return f"{start}–{end}"
        return str(start)
    return "—"


def _ranges_to_start_end(ranges):
    """Возвращает (start, end) как min/max объединения ranges."""
    if not ranges:
        return None, None
    try:
        start = min(s for s, _ in ranges)
        end = max(e for _, e in ranges)
    except Exception:
        return None, None
    return start, end


def _normalize_item_ranges(item: dict) -> None:
    """
    Приводит item к консистентному виду.
    Для v4.0 обновляет ТОЛЬКО "плоские" start/end/ranges (совместимость
    со старым yd_sermon_edit). Категории не трогает.
    """
    ranges = item.get("ranges")
    start = item.get("start")
    end = item.get("end")

    if ranges:
        s2, e2 = _ranges_to_start_end(ranges)
        item["start"] = s2
        item["end"] = e2
        return

    if start is not None and end is not None and start <= end:
        item["ranges"] = [(start, end)]
        return

    item["start"] = None
    item["end"] = None
    item["ranges"] = None


def _is_sermon_slide(item: dict, slide_idx: int) -> bool:
    """Совместимость со старым кодом. Проверяет плоские ranges."""
    ranges = item.get("ranges")
    if ranges:
        return any(s <= slide_idx <= e for s, e in ranges)
    start = item.get("start")
    end = item.get("end")
    return start is not None and start <= slide_idx <= end


def _yd_public_url(disk_path: str) -> str:
    """Строит корректный кликабельный URL Яндекс.Диска."""
    path = disk_path.lstrip("/")
    encoded = urllib.parse.quote(path, safe="/")
    return f"https://disk.yandex.ru/client/disk/{encoded}"


def _format_size(num_bytes: int) -> str:
    """Человекочитаемый размер файла."""
    if num_bytes < 1024:
        return f"{num_bytes} Б"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} КБ"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} МБ"
    return f"{num_bytes / (1024 * 1024 * 1024):.2f} ГБ"


def _get_cancel_keyboard(task_id: str) -> InlineKeyboardBuilder:
    """Клавиатура с единственной кнопкой «Отменить задачу»."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="❌ Отменить задачу",
            callback_data=f"yd_task_cancel:{task_id}",
        ),
    )
    return kb


def _count_slides_for_range(start, end, total):
    """Возвращает количество слайдов в диапазоне [start, end]."""
    if start is None or end is None:
        return 0
    return max(0, min(end, total) - max(start, 1) + 1)


def _count_slides_in_ranges(ranges, total_slides: int) -> int:
    """Считает количество УНИКАЛЬНЫХ слайдов в объединении диапазонов."""
    if not ranges or total_slides <= 0:
        return 0

    unique_slides = set()
    for s, e in ranges:
        lo = max(1, s)
        hi = min(total_slides, e)
        if lo > hi:
            continue
        unique_slides.update(range(lo, hi + 1))
    return len(unique_slides)


# ==========================================
# КАТЕГОРИИ — ЯДРО v4.0
# ==========================================

def _detect_all_categories(
    notes,
    notes_ok: bool,
    incomplete: bool,
) -> Dict[str, dict]:
    """
    Детектит все категории в заметках.
    Возвращает: {cat: {"ranges": ..., "matches": [...], "found": bool}}
    Порядок ключей соответствует _cat_order().
    """
    result: Dict[str, dict] = {}

    if not notes_ok or incomplete or not notes:
        for cat in _cat_order():
            result[cat] = {"ranges": None, "matches": [], "found": False}
        return result

    # Маппинг категория → ключевые слова из config
    kw_map = {
        "opening": getattr(yandex_state.config, "opening_keywords", []),
        "prayer":  getattr(yandex_state.config, "prayer_keywords", []),
        "sermon":  getattr(yandex_state.config, "sermon_keywords", []),
    }

    for cat in _cat_order():
        kws = kw_map.get(cat) or []
        if not kws:
            result[cat] = {"ranges": None, "matches": [], "found": False}
            continue

        try:
            start, end, matches = find_sermon_range(notes, kws)
        except Exception as e:
            logging.error(
                f"[YD-CAT] find_sermon_range({cat}) упал: {e}",
                exc_info=True,
            )
            result[cat] = {"ranges": None, "matches": [], "found": False}
            continue

        # Одна пометка — авто-диапазон не строим
        if matches and len(matches) == 1:
            start, end = None, None

        ranges = [(start, end)] if start is not None else None
        result[cat] = {
            "ranges": ranges,
            "matches": list(matches) if matches else [],
            "found": bool(ranges or matches),
        }

    logging.debug(
        f"[YD-CAT] detect: "
        + ", ".join(
            f"{cat}={'ok' if result[cat]['found'] else 'no'}"
            for cat in _cat_order()
        )
    )
    return result


def _classify_slide(item: dict, slide_idx: int) -> Optional[str]:
    """
    Определяет, к какой категории относится слайд.
    Приоритет = порядок _cat_order() (opening > prayer > sermon).
    Возвращает имя категории или None.
    """
    cats = item.get("categories", {})
    for cat in _cat_order():
        cat_data = cats.get(cat)
        if not cat_data or not cat_data.get("found"):
            continue
        ranges = cat_data.get("ranges") or []
        if any(s <= slide_idx <= e for s, e in ranges):
            return cat
    return None


def _count_category_slides(item: dict) -> Dict[str, int]:
    """
    Возвращает {cat: slide_count, "other": n, "total": total}.
    При пересечении слайд считается только в первую (приоритетную) категорию.
    """
    total = item.get("total_slides", 0)
    counts = {cat: 0 for cat in _cat_order()}
    counts["other"] = 0
    counts["total"] = total

    if total <= 0:
        return counts

    for slide_idx in range(1, total + 1):
        cat = _classify_slide(item, slide_idx)
        if cat is None:
            counts["other"] += 1
        else:
            counts[cat] += 1

    return counts


def _get_default_selected_categories(
    item: dict,
    user_config: dict,
) -> set:
    """
    Возвращает set категорий, отмеченных по умолчанию:
      • found=True И settings_flag=True → в selected
      • "other" → в selected, если process_other=True И other_count > 0
    """
    selected = set()
    cats = item.get("categories", {})
    meta = _cat_meta()

    for cat in _cat_order():
        cat_data = cats.get(cat)
        if not cat_data or not cat_data.get("found"):
            continue
        flag = meta.get(cat, {}).get("settings_flag", f"process_{cat}")
        if user_config.get(flag, True):
            selected.add(cat)

    counts = _count_category_slides(item)
    if user_config.get("process_other", False) and counts.get("other", 0) > 0:
        selected.add("other")

    return selected


# ==========================================
# СПИННЕР ПРОГРЕССА
# ==========================================

_MODE_LABELS = {
    "sermon": "🎯 Только проповедь",
    "other":  "📄 Только остальные",
    "both":   "📦 Проповедь + остальные",
}

# Реестр активных спиннеров: task_id -> (stop_event, spinner_task)
_active_spinners: dict[str, tuple[asyncio.Event, asyncio.Task]] = {}


def _mode_label(mode: str) -> str:
    if mode == "skip":
        return "⏭ Пропущено"
    # v4.0: cat-моды имеют метки из category_meta
    meta = _cat_meta().get(mode)
    if meta:
        return f"{meta['emoji']} {meta['name']}"
    return _MODE_LABELS.get(mode, f"❓ {mode}")


async def _yd_progress_spinner(
    status_msg,
    task_id: str,
    base_text: str,
    stop_event: asyncio.Event,
    interval: float = 1.2,
) -> None:
    """Циклически дописывает '.', '..', '...' в конец статусного сообщения."""
    dots = [".", "..", "..."]
    idx = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass

        if stop_event.is_set():
            return

        try:
            await status_msg.edit_text(
                f"{base_text} {dots[idx]}",
                parse_mode="HTML",
                reply_markup=_get_cancel_keyboard(task_id).as_markup(),
            )
        except asyncio.CancelledError:
            raise
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                logging.debug(f"[YD-SPINNER] TelegramBadRequest: {e}")
        except Exception as e:
            logging.debug(f"[YD-SPINNER] edit_text: {e}")

        idx = (idx + 1) % len(dots)


async def _yd_stop_spinner(task_id: str, wait_timeout: float = 2.0) -> None:
    """Останавливает активный спиннер задачи."""
    entry = _active_spinners.pop(task_id, None)
    if entry is None:
        return
    stop_event, spinner_task = entry
    stop_event.set()
    try:
        await asyncio.wait_for(asyncio.shield(spinner_task), timeout=wait_timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        spinner_task.cancel()
        try:
            await spinner_task
        except (asyncio.CancelledError, Exception):
            pass
    except Exception as e:
        logging.debug(f"[YD-SPINNER] stop: {e}")


async def _yd_with_spinner(status_msg, task_id: str, base_text: str, coro):
    """Запускает корутину, параллельно анимируя статусное сообщение."""
    stop_event = asyncio.Event()
    spinner_task = asyncio.create_task(
        _yd_progress_spinner(status_msg, task_id, base_text, stop_event)
    )
    _active_spinners[task_id] = (stop_event, spinner_task)
    try:
        return await coro
    finally:
        entry = _active_spinners.get(task_id)
        if entry is not None and entry[1] is spinner_task:
            _active_spinners.pop(task_id, None)
        stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(spinner_task), timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            spinner_task.cancel()
            try:
                await spinner_task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception as e:
            logging.debug(f"[YD-SPINNER] finalize: {e}")


# ==========================================
# ЗАЩИТА IN-FLIGHT ОПЕРАЦИЙ
# ==========================================

_active_ops: dict[str, set[asyncio.Task]] = {}
_deferred_cleanup_tasks: set[asyncio.Task] = set()
_deferred_dirs: set[Path] = set()
_cleaning_tasks: set[str] = set()
_detached_workers: set[asyncio.Task] = set()


async def _yd_run_protected(task_id: str, coro):
    """Запускает корутину как Task, регистрирует в _active_ops и защищает shield."""
    async def _runner():
        try:
            return await coro
        finally:
            bucket = _active_ops.get(task_id)
            if bucket is not None:
                bucket.discard(asyncio.current_task())
                if not bucket:
                    _active_ops.pop(task_id, None)

    op_task = asyncio.create_task(_runner())
    _active_ops.setdefault(task_id, set()).add(op_task)
    return await asyncio.shield(op_task)


async def _yd_to_thread(task_id: str, fn, *args, **kwargs):
    """asyncio.to_thread + регистрация в _active_ops."""
    return await _yd_run_protected(
        task_id, asyncio.to_thread(fn, *args, **kwargs)
    )


async def _yd_async_protected(task_id: str, coro):
    """Защита сетевых корутин Яндекса от внешней отмены."""
    async def _runner():
        try:
            return await coro
        finally:
            bucket = _active_ops.get(task_id)
            if bucket is not None:
                bucket.discard(asyncio.current_task())
                if not bucket:
                    _active_ops.pop(task_id, None)

    op_task = asyncio.create_task(_runner())
    _active_ops.setdefault(task_id, set()).add(op_task)
    return await asyncio.shield(op_task)


async def _yd_deferred_task_dir_cleanup(task_dir: Path, ops: list) -> None:
    """Фоновое удаление task_dir: ждём завершения in-flight ops."""
    try:
        await asyncio.gather(*ops, return_exceptions=True)
    except Exception as e:
        logging.debug(f"[YD-DEFERRED] gather: {e}")
    _safe_delete_task_dir(task_dir)


def _spawn_deferred_cleanup(task_dir: Path, ops: list) -> None:
    """Создаёт deferred cleanup task с защитой от дублирования по папке."""
    if task_dir is None:
        return
    if task_dir in _deferred_dirs:
        logging.info(f"[YD-DEFERRED] {task_dir} уже в очереди — skip")
        return
    _deferred_dirs.add(task_dir)

    task = asyncio.create_task(_yd_deferred_task_dir_cleanup(task_dir, ops))
    _deferred_cleanup_tasks.add(task)

    def _done(t: asyncio.Task):
        _deferred_cleanup_tasks.discard(t)
        _deferred_dirs.discard(task_dir)

    task.add_done_callback(_done)


async def _cancel_worker_async(
    worker: asyncio.Task,
    task_id: str,
    timeout: float = 15.0,
) -> None:
    """Фоновая отмена worker'а."""
    if worker is None or worker.done() or worker is asyncio.current_task():
        return
    try:
        worker.cancel()
    except Exception as e:
        logging.debug(f"[YD-CANCEL-ASYNC] cancel {task_id}: {e}")
    try:
        await asyncio.wait_for(asyncio.shield(worker), timeout=timeout)
        logging.info(f"[YD-CANCEL-ASYNC] worker {task_id} завершён")
    except asyncio.TimeoutError:
        logging.warning(
            f"[YD-CANCEL-ASYNC] worker {task_id} не завершился за {timeout}s"
        )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.debug(f"[YD-CANCEL-ASYNC] await {task_id}: {e}")


def _spawn_cancel_worker(worker: asyncio.Task, task_id: str) -> None:
    """Запускает фоновую отмену worker'а."""
    t = asyncio.create_task(_cancel_worker_async(worker, task_id))
    _deferred_cleanup_tasks.add(t)
    t.add_done_callback(_deferred_cleanup_tasks.discard)


async def _run_worker_detached(
    task_id: str,
    coro_factory: Callable[[], Awaitable[None]],
    *,
    name: str = "yd_worker",
) -> asyncio.Task:
    """Запускает worker в отдельном task'е, регистрирует в session, возвращает сразу."""
    async def _wrapper():
        try:
            await coro_factory()
        except asyncio.CancelledError:
            logging.info(f"[YD-WORKER] {name} {task_id} отменён")
        except Exception as e:
            logging.error(
                f"[YD-WORKER] {name} {task_id} упал: {e}", exc_info=True
            )

    worker = asyncio.create_task(_wrapper())
    _detached_workers.add(worker)

    def _done(t: asyncio.Task):
        _detached_workers.discard(t)
        sess = sessions.get(task_id)
        if sess is not None and sess.get("worker_task") is t:
            sess["worker_task"] = None

    worker.add_done_callback(_done)

    async with yd_session_lock:
        sess = sessions.get(task_id)
        if sess is not None:
            sess["worker_task"] = worker

    return worker


async def drain_deferred_cleanups(timeout: float = 30.0) -> None:
    """Дожидается завершения отложенных cleanup-задач и detached worker'ов."""
    pending_cleanups = [t for t in _deferred_cleanup_tasks if not t.done()]
    pending_workers = [t for t in _detached_workers if not t.done()]

    if not pending_cleanups and not pending_workers:
        return

    logging.info(
        f"[YD-DRAIN] Ожидаю {len(pending_cleanups)} cleanup-задач "
        f"и {len(pending_workers)} detached worker'ов..."
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(
                *pending_cleanups, *pending_workers,
                return_exceptions=True,
            ),
            timeout=timeout,
        )
        logging.info("[YD-DRAIN] Все отложенные задачи завершены")
    except asyncio.TimeoutError:
        logging.warning(
            f"[YD-DRAIN] Не все задачи завершились за {timeout}s"
        )


# ==========================================
# ПАЙПЛАЙН ПОДГОТОВКИ (без конвертации)
# ==========================================

async def _yd_prepare_files_impl(
    callback: types.CallbackQuery,
    bot: Bot,
    SHM_DIR: str,
    user_mgr,
    files_to_process: list,
    sunday,
    paths: dict,
    session_key: str,
    nonce: str,
):
    """Первая фаза: скачивание + извлечение заметок + детект категорий."""
    status_msg = callback.message
    owner_user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    task_id = f"yd_task_{secrets.token_hex(6)}"
    task_dir = Path(SHM_DIR) / task_id

    registration_status = "ok"

    async with yd_session_lock:
        picker = sessions.get(session_key)
        if picker is None:
            registration_status = "no_picker"
        elif picker.get("nonce") != nonce:
            logging.warning(
                f"[YD-PREP] picker.nonce={picker.get('nonce')!r} ≠ "
                f"expected nonce={nonce!r} — прерываем"
            )
            registration_status = "wrong_nonce"
        else:
            picker.setdefault("task_ids", []).append(task_id)
            picker["processing"] = False
            sessions[task_id] = {
                "user_id": owner_user_id,
                "chat_id": chat_id,
                "session_key": session_key,
                "pending": None,
                "nonce": nonce,
                "created_at": time.time(),
                "cancelled": False,
                "worker_task": None,
            }
            yd_active_tasks.add(task_id)

    if registration_status == "no_picker":
        await _safe_edit(status_msg, "❌ Сессия была отменена.")
        return

    if registration_status == "wrong_nonce":
        await _safe_edit(
            status_msg,
            "❌ Сессия была заменена другой командой.\n"
            "Запустите /sunday заново."
        )
        return

    try:
        task_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.error(
            f"[YD-PREP] mkdir упал для {task_id}: {e}", exc_info=True,
        )
        try:
            async with yd_session_lock:
                picker = sessions.get(session_key)
                if picker is not None:
                    task_ids = picker.get("task_ids")
                    if isinstance(task_ids, list) and task_id in task_ids:
                        task_ids.remove(task_id)
                sessions.pop(task_id, None)
                yd_active_tasks.discard(task_id)
        except Exception as cleanup_err:
            logging.error(
                f"[YD-PREP] не удалось откатить регистрацию: {cleanup_err}",
                exc_info=True,
            )
        _safe_delete_task_dir(task_dir)
        await _safe_edit(
            status_msg,
            f"❌ Не удалось создать папку задачи: "
            f"<code>{html_module.escape(str(e)[:200])}</code>",
        )
        return

    status_message_id = status_msg.message_id if status_msg is not None else None

    logging.info(
        f"[YD-PREP] Старт: task_id={task_id}, файлов={len(files_to_process)}"
    )

    cleanup_done = False
    try:
        target_base = paths["target"]
        user_config = user_mgr.get_user_config(owner_user_id)
        quality = user_config.get("quality", "2k")

        prepared = []

        for f_idx, pptx_item in enumerate(files_to_process, start=1):
            _touch_task(task_dir)

            task_sess = sessions.get(task_id)
            if task_sess is None or task_sess.get("cancelled"):
                logging.info(
                    f"[YD-PREP] Задача {task_id} отменена — прерываем"
                )
                cleanup_done = True
                await asyncio.shield(_yd_cleanup_task(
                    task_id, session_key, task_dir,
                    owner_user_id, chat_id, nonce,
                ))
                return

            file_name = pptx_item["name"]
            file_name_esc = html_module.escape(file_name)
            file_size = pptx_item.get("size", 0)

            logging.info(
                f"[YD-PREP] Файл #{f_idx}/{len(files_to_process)}: "
                f"{file_name!r} ({_format_size(file_size)})"
            )

            per_file_dir = task_dir / f"src_{f_idx}"
            per_file_dir.mkdir(exist_ok=True)

            base_dl_text = f"📥 Скачиваю <code>{file_name_esc}</code>"
            await _safe_edit(
                status_msg,
                base_dl_text,
                parse_mode="HTML",
                reply_markup=_get_cancel_keyboard(task_id).as_markup(),
            )

            local_pptx = per_file_dir / file_name
            ok = await _yd_with_spinner(
                status_msg,
                task_id,
                base_dl_text,
                _yd_async_protected(
                    task_id,
                    yandex_state.config.client.download_file(
                        pptx_item["path"], local_pptx
                    ),
                ),
            )
            if not ok:
                logging.error(f"[YD-PREP] {file_name}: скачивание провалилось")
                prepared.append({
                    "file_name": file_name,
                    "file_slug": safe_folder_name(file_name),
                    "failed_at_stage": "download",
                })
                continue

            logging.debug(
                f"[YD-PREP] {file_name}: скачан, "
                f"размер={_format_size(local_pptx.stat().st_size)}"
            )

            task_sess = sessions.get(task_id)
            if task_sess is None or task_sess.get("cancelled"):
                logging.info(
                    f"[YD-PREP] Задача {task_id} отменена после скачивания — прерываем"
                )
                cleanup_done = True
                await asyncio.shield(_yd_cleanup_task(
                    task_id, session_key, task_dir,
                    owner_user_id, chat_id, nonce,
                ))
                return

            if local_pptx.suffix.lower() == ".ppt":
                base_norm_text = (
                    f"🔧 Готовлю .ppt → .pptx: <code>{file_name_esc}</code>"
                )
                await _safe_edit(
                    status_msg,
                    base_norm_text,
                    parse_mode="HTML",
                    reply_markup=_get_cancel_keyboard(task_id).as_markup(),
                )
                try:
                    normalized_pptx = await _yd_with_spinner(
                        status_msg,
                        task_id,
                        base_norm_text,
                        _yd_to_thread(
                            task_id,
                            ppt_to_pptx_crossplatform,
                            local_pptx, per_file_dir,
                        ),
                    )
                except Exception as e:
                    logging.error(
                        f"[YD-PREP] {file_name}: .ppt→.pptx упал: {e}",
                        exc_info=True,
                    )
                    prepared.append({
                        "file_name": file_name,
                        "file_slug": safe_folder_name(file_name),
                        "failed_at_stage": "normalize",
                    })
                    continue

                if not normalized_pptx or not Path(normalized_pptx).exists():
                    logging.error(f"[YD-PREP] {file_name}: .pptx не создан")
                    prepared.append({
                        "file_name": file_name,
                        "file_slug": safe_folder_name(file_name),
                        "failed_at_stage": "normalize",
                    })
                    continue
            else:
                normalized_pptx = local_pptx

            task_sess = sessions.get(task_id)
            if task_sess is None or task_sess.get("cancelled"):
                logging.info(
                    f"[YD-PREP] Задача {task_id} отменена после нормализации — прерываем"
                )
                cleanup_done = True
                await asyncio.shield(_yd_cleanup_task(
                    task_id, session_key, task_dir,
                    owner_user_id, chat_id, nonce,
                ))
                return

            total_slides = None
            try:
                from pptx import Presentation
                prs = Presentation(str(normalized_pptx))
                total_slides = len(prs.slides._sldIdLst)
            except Exception as e:
                logging.warning(
                    f"[YD-PREP] {file_name}: python-pptx не открыл: {e}"
                )

            if not total_slides:
                renorm_result = await _yd_to_thread(
                    task_id,
                    librenormalize_to_pptx, normalized_pptx, per_file_dir,
                )
                if renorm_result is None:
                    logging.error(
                        f"[YD-PREP] {file_name}: файл не читается"
                    )
                    prepared.append({
                        "file_name": file_name,
                        "file_slug": safe_folder_name(file_name),
                        "failed_at_stage": "count",
                    })
                    continue
                normalized_pptx, total_slides = renorm_result
                logging.info(
                    f"[YD-PREP] {file_name}: нормализован через LibreOffice "
                    f"({total_slides} слайдов)"
                )

            if not total_slides:
                logging.error(
                    f"[YD-PREP] {file_name}: total_slides == 0, пропускаем"
                )
                prepared.append({
                    "file_name": file_name,
                    "file_slug": safe_folder_name(file_name),
                    "failed_at_stage": "count",
                })
                continue

            await _safe_edit(
                status_msg,
                f"🔍 Читаю заметки докладчика: <code>{file_name_esc}</code>...",
                parse_mode="HTML",
                reply_markup=_get_cancel_keyboard(task_id).as_markup(),
            )

            notes_ok, notes, incomplete = await _yd_to_thread(
                task_id, extract_speaker_notes, str(normalized_pptx)
            )

            # ✅ v4.0: детектим все категории
            categories = _detect_all_categories(
                notes, notes_ok, incomplete
            )

            if not notes_ok:
                incomplete_warning = (
                    "⚠️ Не удалось прочитать заметки докладчика."
                )
            elif incomplete:
                incomplete_warning = (
                    "⚠️ Заметки прочитаны частично, "
                    "категории не определены автоматически."
                )
            else:
                incomplete_warning = None

            logging.info(
                f"[YD-PREP] {file_name}: заметок={len(notes)}, "
                f"categories="
                + ", ".join(
                    f"{c}({categories[c]['found']})"
                    for c in _cat_order()
                )
                + f", notes_ok={notes_ok}, incomplete={incomplete}"
            )

            # Совместимость со старым кодом: плоские ranges/start/end
            # = sermon (для yd_sermon_edit и handlers.py)
            sermon_data = categories.get("sermon") or {}
            legacy_ranges = sermon_data.get("ranges")
            legacy_start, legacy_end = _ranges_to_start_end(legacy_ranges)

            item = {
                "file_name": file_name,
                "file_slug": safe_folder_name(file_name),
                "file_path": normalized_pptx,
                "total_slides": total_slides,
                # ✅ v4.0: категории
                "categories": categories,
                # Совместимость со старым
                "start": legacy_start,
                "end": legacy_end,
                "ranges": legacy_ranges,
                "matches": sermon_data.get("matches", []),
                "notes_ok": notes_ok,
                "incomplete": incomplete,
                "incomplete_warning": incomplete_warning,
                "confirmed": False,
                "convert_mode": None,
                # ✅ v4.0: дефолтные галки и конфиг пользователя
                "selected_categories": None,  # заполним при рендере
                "_user_config": dict(user_config),
            }

            # Дефолтные галки
            item["selected_categories"] = _get_default_selected_categories(
                item, user_config
            )

            prepared.append(item)

        cleanup_needed = False
        async with yd_session_lock:
            picker = sessions.get(session_key)
            if picker is None or picker.get("cancelled"):
                cleanup_needed = True
            else:
                task_sess = sessions.get(task_id)
                if task_sess is None or task_sess.get("cancelled"):
                    cleanup_needed = True
                else:
                    task_sess["pending"] = {
                        "task_dir": task_dir,
                        "target_base": target_base,
                        "quality": quality,
                        "prepared": prepared,
                        "owner_user_id": owner_user_id,
                        "chat_id": chat_id,
                        "session_key": session_key,
                        "nonce": nonce,
                        "bot": bot,
                        "prompt_nonce": None,
                        "prompt_idx": None,
                        "prompt_message_id": None,
                        "prompt_timeout_task": None,
                        "prompt_watchdog_nonce": None,
                        "status_message_id": status_message_id,
                        "awaiting_range_for_idx": None,
                    }

        if cleanup_needed:
            logging.info(
                f"[YD-PREP] Задача {task_id} отменена до сохранения pending"
            )
            cleanup_done = True
            await asyncio.shield(_yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
            ))
            return

        needs_confirm = [
            p for p in prepared
            if "file_path" in p and not p.get("confirmed")
        ]

        logging.info(
            f"[YD-PREP] Готово: prepared={len(prepared)}, "
            f"needs_confirm={len(needs_confirm)}"
        )

        if not needs_confirm:
            # Все файлы уже подтверждены (не должно случаться на новом коде)
            for item in prepared:
                if not item.get("selected_categories"):
                    item["selected_categories"] = {"other"} if "file_path" in item else set()
                item["confirmed"] = True
            await _run_worker_detached(
                task_id,
                lambda: _yd_convert_and_upload_impl(
                    bot=bot,
                    task_id=task_id,
                    status_msg=status_msg,
                ),
                name="convert",
            )
            return

        await _yd_render_category_prompt(
            task_id=task_id,
            item=needs_confirm[0],
            status_msg=status_msg,
        )

    except asyncio.CancelledError:
        logging.info(
            f"[YD-PREP] Worker {task_id} отменён — выполняю cleanup"
        )
        cleanup_done = True
        try:
            await asyncio.shield(_yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
            ))
        except Exception as e:
            logging.error(
                f"[YD-PREP] cleanup после отмены упал: {e}", exc_info=True
            )
        raise

    except Exception as e:
        logging.error(f"[YD-PREP] Ошибка: {e}", exc_info=True)
        cleanup_done = True
        await _yd_cleanup_task(
            task_id, session_key, task_dir,
            owner_user_id, chat_id, nonce,
            bot=bot, status_msg=status_msg, error=e,
        )


async def _yd_prepare_files(
    callback: types.CallbackQuery,
    bot: Bot,
    SHM_DIR: str,
    user_mgr,
    files_to_process: list,
    sunday,
    paths: dict,
    session_key: str,
    nonce: str,
) -> asyncio.Task:
    """Запускает _yd_prepare_files_impl как detached worker."""
    temp_task_id = f"yd_pending_{secrets.token_hex(4)}"

    async def _factory():
        await _yd_prepare_files_impl(
            callback=callback,
            bot=bot,
            SHM_DIR=SHM_DIR,
            user_mgr=user_mgr,
            files_to_process=files_to_process,
            sunday=sunday,
            paths=paths,
            session_key=session_key,
            nonce=nonce,
        )

    return await _run_worker_detached(
        temp_task_id, _factory, name="prepare",
    )


# ==========================================
# ПРОМПТЫ — СТАРЫЙ (fallback) и НОВЫЙ (v4.0)
# ==========================================

async def _yd_claim_prompt(callback: types.CallbackQuery) -> Optional[dict]:
    """Атомарно проверяет и 'потребляет' промпт."""
    parts = callback.data.split(":")
    if len(parts) < 4:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return None
    task_id = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return None
    nonce = parts[3]

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return None
    if session.get("cancelled"):
        await _safe_answer(callback, "❌ Задача отменена.", show_alert=True)
        return None

    pending = session["pending"]
    if not isinstance(pending, dict):
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return None

    if pending.get("prompt_nonce") != nonce or pending.get("prompt_idx") != idx:
        await _safe_answer(callback, "⏳ Промпт уже обработан.", show_alert=True)
        return None

    if callback.from_user.id != pending["owner_user_id"]:
        await _safe_answer(callback, "❌ Только автор.", show_alert=True)
        return None

    timeout_task = pending.get("prompt_timeout_task")
    if timeout_task is not None and not timeout_task.done():
        timeout_task.cancel()
    pending["prompt_timeout_task"] = None
    pending["prompt_watchdog_nonce"] = None

    pending["prompt_nonce"] = None
    pending["prompt_idx"] = None
    pending["prompt_message_id"] = None
    pending["awaiting_range_for_idx"] = None

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logging.debug(f"claim_prompt edit_reply_markup: {e}")
    except Exception as e:
        logging.debug(f"claim_prompt edit_reply_markup: {e}")

    return pending


# ------------------------------------------------------------
# НОВЫЙ ПРОМПТ v4.0 — toggle-клавиатура категорий
# ------------------------------------------------------------

def _render_category_toggle_keyboard(
    task_id: str, idx: int, nonce: str,
    item: dict,
    selected: set,
    total_slides: int,
) -> InlineKeyboardBuilder:
    """
    Клавиатура с toggle-кнопками категорий.

    Для каждой категории (opening, prayer, sermon):
      ✅ 🌅 Начало (1–5, 5 с.)       [если found и в selected]
      ⬜ 🌅 Начало (1–5, 5 с.)       [если found и не в selected]
      ⚫ 🌅 Начало — не найдено       [если !found, disabled]

    «Остальные»:
      ✅ 📄 Остальные (194 с.)        [если в selected]
      ⬜ 📄 Остальные (194 с.)        [если не в selected и > 0]

    Плюс:
      ✏️ Изменить диапазон категории
      ✅ Конвертировать выбранные (N с.)
      ❌ Отменить
    """
    kb = InlineKeyboardBuilder()
    counts = _count_category_slides(item)
    cats = item.get("categories", {})
    meta = _cat_meta()

    for cat in _cat_order():
        cat_meta = meta.get(cat, {})
        emoji = cat_meta.get("emoji", "❓")
        name = cat_meta.get("name", cat)
        cat_data = cats.get(cat) or {}
        found = bool(cat_data.get("found"))
        count = counts.get(cat, 0)

        if not found:
            kb.row(InlineKeyboardButton(
                text=f"⚫ {emoji} {name} — не найдено",
                callback_data=f"yd_cat_noop:{task_id}:{idx}:{nonce}:{cat}",
            ))
            continue

        ranges = cat_data.get("ranges") or []
        ranges_text = _format_ranges_text(ranges)
        check = "✅" if cat in selected else "⬜"
        if count > 0:
            text = f"{check} {emoji} {name} ({ranges_text}, {count} с.)"
        else:
            text = f"{check} {emoji} {name} ({ranges_text})"

        kb.row(InlineKeyboardButton(
            text=text,
            callback_data=f"yd_cat_toggle:{task_id}:{idx}:{nonce}:{cat}",
        ))

    # Остальные
    other_count = counts.get("other", 0)
    if other_count > 0:
        check = "✅" if "other" in selected else "⬜"
        kb.row(InlineKeyboardButton(
            text=f"{check} 📄 Остальные ({other_count} с.)",
            callback_data=f"yd_cat_toggle:{task_id}:{idx}:{nonce}:other",
        ))

    # Изменить диапазон
    kb.row(InlineKeyboardButton(
        text="✏️ Изменить диапазон категории",
        callback_data=f"yd_cat_edit:{task_id}:{idx}:{nonce}",
    ))

    # Итоговое количество слайдов
    total_selected = 0
    for cat in _cat_order():
        if cat in selected:
            total_selected += counts.get(cat, 0)
    if "other" in selected:
        total_selected += other_count

    # Конвертировать (если что-то выбрано)
    if total_selected > 0:
        kb.row(InlineKeyboardButton(
            text=f"✅ Конвертировать выбранные ({total_selected} с.)",
            callback_data=f"yd_cat_convert:{task_id}:{idx}:{nonce}",
        ))
    else:
        kb.row(InlineKeyboardButton(
            text="✅ Конвертировать выбранные",
            callback_data=f"yd_cat_noop:{task_id}:{idx}:{nonce}:empty",
        ))

    kb.row(InlineKeyboardButton(
        text="❌ Отменить задачу",
        callback_data=f"yd_task_cancel:{task_id}",
    ))

    return kb


def _render_category_prompt_text(item: dict, selected: set) -> str:
    """Формирует текст промпта категорий."""
    file_name = item.get("file_name", "")
    file_esc = html_module.escape(file_name)
    total_slides = item.get("total_slides", 0)
    cats = item.get("categories", {})
    meta = _cat_meta()
    counts = _count_category_slides(item)

    lines = [
        f"🎬 <b>Выбор категорий для конвертации</b>",
        "",
        f"📄 Файл: <code>{file_esc}</code>",
        f"📌 Всего слайдов: <b>{total_slides}</b>",
        "",
    ]

    # Информация по найденным
    found_lines = []
    for cat in _cat_order():
        cat_data = cats.get(cat) or {}
        if not cat_data.get("found"):
            continue
        cat_meta = meta.get(cat, {})
        emoji = cat_meta.get("emoji", "❓")
        name = cat_meta.get("name", cat)
        count = counts.get(cat, 0)
        ranges = cat_data.get("ranges") or []
        ranges_text = _format_ranges_text(ranges)
        manual = " (вручную)" if cat_data.get("manual") else ""
        found_lines.append(
            f"{emoji} {name}{manual}: <b>{ranges_text}</b> ({count} с.)"
        )

    if found_lines:
        lines.extend(found_lines)
        lines.append("")
    else:
        lines.append("⚠️ <i>Ни одна категория не найдена автоматически.</i>")
        lines.append("")

    # Про остальные
    other_count = counts.get("other", 0)
    if other_count > 0:
        lines.append(f"📄 Остальные: <b>{other_count} с.</b>")
        lines.append("")

    lines.append("🎯 <b>Отметьте категории для конвертации:</b>")

    # Предупреждение, если ничего не отмечено
    if not selected:
        lines.append("")
        lines.append(
            "⚠️ <i>Ничего не выбрано. Отметьте хотя бы одну категорию.</i>"
        )

    return "\n".join(lines)


async def _yd_render_category_prompt(
    task_id: str,
    item: dict,
    status_msg,
    reply_fn=None,
) -> None:
    """Рендер нового промпта категорий."""
    session = sessions.get(task_id)
    if not session or "pending" not in session:
        return
    if session.get("cancelled"):
        return
    pending = session["pending"]
    if not isinstance(pending, dict):
        return

    try:
        idx = pending["prepared"].index(item)
    except (ValueError, KeyError):
        logging.error(
            f"_yd_render_category_prompt: item не найден для {task_id}"
        )
        return

    pending["prompt_idx"] = idx

    if pending.get("prompt_nonce") is None:
        pending["prompt_nonce"] = secrets.token_hex(4)
    prompt_nonce = pending["prompt_nonce"]

    total_slides = item.get("total_slides", 0)
    if total_slides == 0:
        file_path = item.get("file_path")
        if file_path and Path(file_path).exists():
            try:
                from pptx import Presentation
                prs = Presentation(str(file_path))
                total_slides = len(prs.slides._sldIdLst)
                item["total_slides"] = total_slides
            except Exception as e:
                logging.warning(
                    f"Не удалось определить число слайдов: {e}"
                )

    # Дефолтные галки, если ещё не заданы
    if item.get("selected_categories") is None:
        item["selected_categories"] = _get_default_selected_categories(
            item, item.get("_user_config", {})
        )
    selected = set(item.get("selected_categories") or set())

    text = _render_category_prompt_text(item, selected)
    kb = _render_category_toggle_keyboard(
        task_id, idx, prompt_nonce, item, selected, total_slides
    )

    sent_msg = None
    if reply_fn is not None:
        sent_msg = await reply_fn(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
    elif status_msg is not None:
        await status_msg.edit_text(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
        sent_msg = status_msg
    else:
        logging.warning(
            f"_yd_render_category_prompt: нет ни reply_fn, ни status_msg "
            f"для {task_id}"
        )
        return

    if pending.get("prompt_nonce") != prompt_nonce:
        logging.info(
            f"_yd_render_category_prompt: nonce изменён конкурентно"
        )
        return

    if sent_msg is not None and hasattr(sent_msg, "message_id"):
        pending["prompt_message_id"] = sent_msg.message_id

    # Watchdog
    existing_task = pending.get("prompt_timeout_task")
    existing_nonce = pending.get("prompt_watchdog_nonce")

    if existing_task is not None and not existing_task.done():
        if existing_nonce == prompt_nonce:
            return
        existing_task.cancel()
        pending["prompt_timeout_task"] = None
        pending["prompt_watchdog_nonce"] = None

    pending["prompt_timeout_task"] = asyncio.create_task(
        _yd_prompt_timeout_watchdog(
            task_id, yandex_state.config.prompt_timeout_sec, prompt_nonce
        )
    )
    pending["prompt_watchdog_nonce"] = prompt_nonce


# ------------------------------------------------------------
# СТАРЫЙ ПРОМПТ (fallback для yd_sermon_edit и handlers.py)
# ------------------------------------------------------------

async def _yd_render_sermon_prompt(
    task_id: str,
    item: dict,
    status_msg,
    reply_fn=None,
) -> None:
    """
    Старый промпт с 3 кнопками (sermon / other / both).
    Используется:
      • handlers.py после ручного ввода диапазона проповеди;
      • как fallback, если v4.0-промпт недоступен.
    """
    session = sessions.get(task_id)
    if not session or "pending" not in session:
        return
    if session.get("cancelled"):
        return
    pending = session["pending"]
    if not isinstance(pending, dict):
        return

    try:
        idx = pending["prepared"].index(item)
    except (ValueError, KeyError):
        logging.error(
            f"_yd_render_sermon_prompt: item не найден для {task_id}"
        )
        return

    pending["prompt_idx"] = idx

    if pending.get("prompt_nonce") is None:
        pending["prompt_nonce"] = secrets.token_hex(4)
    prompt_nonce = pending["prompt_nonce"]

    _normalize_item_ranges(item)

    matches = item.get("matches", []) or []
    start = item.get("start")
    end = item.get("end")
    ranges = item.get("ranges")
    file_name = item.get("file_name", "")
    file_esc = html_module.escape(file_name)

    total_slides = item.get("total_slides", 0)
    if total_slides == 0:
        file_path = item.get("file_path")
        if file_path and Path(file_path).exists():
            try:
                from pptx import Presentation
                prs = Presentation(str(file_path))
                total_slides = len(prs.slides._sldIdLst)
            except Exception as e:
                logging.warning(f"Не удалось определить число слайдов: {e}")

    kb = InlineKeyboardBuilder()

    has_valid_range = (
        bool(ranges)
        or (start is not None and end is not None and start <= end)
    )

    if has_valid_range:
        if ranges:
            sermon_count = _count_slides_in_ranges(ranges, total_slides)
            ranges_text = _format_ranges_text(ranges, start, end)
        else:
            sermon_count = _count_slides_for_range(start, end, total_slides)
            ranges_text = _format_ranges_text(None, start, end)

        other_count = max(0, total_slides - sermon_count)

        manual_range = item.get("manual_range", False)

        if manual_range:
            text = (
                f"🎯 <b>Диапазон проповеди установлен</b>\n\n"
                f"📄 Файл: <code>{file_esc}</code>\n"
                f"📊 Диапазон: <b>{ranges_text}</b>\n"
                f"🎯 Слайдов проповеди: <b>{sermon_count}</b>\n\n"
                f"❓ <b>Какие слайды конвертировать?</b>"
            )
        else:
            preview = ", ".join(str(n) for n in matches[:15])
            if len(matches) > 15:
                preview += f" …и ещё {len(matches) - 15}"

            text = (
                f"🎯 <b>Найдена пометка «проповедь»</b>\n\n"
                f"📄 Файл: <code>{file_esc}</code>\n"
                f"📌 Слайды с пометкой: <code>{preview}</code>\n"
                f"📊 Предлагаемый диапазон: <b>{ranges_text}</b>\n\n"
                f"❓ <b>Какие слайды конвертировать?</b>"
            )

        kb.row(
            InlineKeyboardButton(
                text=f"✅ Только проповедь ({sermon_count} с.)",
                callback_data=(
                    f"yd_sermon_mode:{task_id}:{idx}:{prompt_nonce}:sermon"
                ),
            ),
        )
        kb.row(
            InlineKeyboardButton(
                text=f"📄 Только остальные ({other_count} с.)",
                callback_data=(
                    f"yd_sermon_mode:{task_id}:{idx}:{prompt_nonce}:other"
                ),
            ),
        )
        kb.row(
            InlineKeyboardButton(
                text=f"📦 Проповеди и остальное раздельно ({total_slides} с.)",
                callback_data=(
                    f"yd_sermon_mode:{task_id}:{idx}:{prompt_nonce}:both"
                ),
            ),
        )
        kb.row(
            InlineKeyboardButton(
                text="✏️ Изменить диапазон Проповеди",
                callback_data=f"yd_sermon_edit:{task_id}:{idx}:{prompt_nonce}",
            ),
        )
    else:
        notes_ok = item.get("notes_ok", True)
        incomplete = item.get("incomplete", False)

        if not notes_ok:
            reason = (
                "⚠️ <b>Не удалось прочитать заметки докладчика.</b>\n"
                "Возможно, файл повреждён или содержит только изображения."
            )
        elif incomplete:
            reason = (
                "⚠️ <b>Заметки прочитаны частично.</b>\n"
                "Автоматически определить проповедь не удалось."
            )
        elif matches:
            match_str = ", ".join(str(n) for n in matches[:5])
            reason = (
                f"📌 Найдена <b>одна</b> пометка «проповедь»: "
                f"слайд <code>{match_str}</code>\n"
                f"Для одной пометки авто-диапазон не строится."
            )
        else:
            reason = "В заметках докладчика нет слова «проповедь»."

        text = (
            f"🤔 <b>Автоматически определить проповедь не удалось</b>\n\n"
            f"📄 Файл: <code>{file_esc}</code>\n"
            f"📌 Всего слайдов: <b>{total_slides}</b>\n\n"
            f"{reason}\n\n"
            f"❓ <b>Что делать с файлом?</b>"
        )

        kb.row(
            InlineKeyboardButton(
                text=f"📄 Конвертировать всё ({total_slides} с.)",
                callback_data=(
                    f"yd_sermon_mode:{task_id}:{idx}:{prompt_nonce}:other"
                ),
            ),
        )
        kb.row(
            InlineKeyboardButton(
                text="✏️ Указать диапазон Проповеди",
                callback_data=f"yd_sermon_edit:{task_id}:{idx}:{prompt_nonce}",
            ),
        )

    kb.row(
        InlineKeyboardButton(
            text="❌ Отменить задачу",
            callback_data=f"yd_task_cancel:{task_id}",
        ),
    )

    sent_msg = None
    if reply_fn is not None:
        sent_msg = await reply_fn(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
    elif status_msg is not None:
        await status_msg.edit_text(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
        sent_msg = status_msg
    else:
        logging.warning(
            f"_yd_render_sermon_prompt: нет ни reply_fn, ни status_msg "
            f"для {task_id}"
        )
        return

    if pending.get("prompt_nonce") != prompt_nonce:
        logging.info(
            f"_yd_render_sermon_prompt: nonce изменён конкурентно"
        )
        return

    if sent_msg is not None and hasattr(sent_msg, "message_id"):
        pending["prompt_message_id"] = sent_msg.message_id

    existing_task = pending.get("prompt_timeout_task")
    existing_nonce = pending.get("prompt_watchdog_nonce")

    if existing_task is not None and not existing_task.done():
        if existing_nonce == prompt_nonce:
            return
        existing_task.cancel()
        pending["prompt_timeout_task"] = None
        pending["prompt_watchdog_nonce"] = None

    pending["prompt_timeout_task"] = asyncio.create_task(
        _yd_prompt_timeout_watchdog(
            task_id, yandex_state.config.prompt_timeout_sec, prompt_nonce
        )
    )
    pending["prompt_watchdog_nonce"] = prompt_nonce


async def _yd_prompt_timeout_watchdog(
    task_id: str, timeout_sec: int, expected_nonce: str
):
    """Если пользователь не ответил — уведомляем и очищаем."""
    try:
        await asyncio.sleep(timeout_sec)

        snapshot = None
        async with yd_session_lock:
            session = sessions.get(task_id)
            if not session or "pending" not in session:
                return
            pending = session["pending"]
            if not isinstance(pending, dict):
                return
            if pending.get("prompt_nonce") != expected_nonce:
                logging.info(
                    f"[YD-PROMPT] watchdog {task_id}: nonce устарел — skip"
                )
                return
            snapshot = {
                "chat_id": pending.get("chat_id"),
                "bot": pending.get("bot"),
                "owner_user_id": pending.get("owner_user_id"),
                "session_key": pending.get("session_key"),
                "task_dir": pending.get("task_dir"),
                "nonce": pending.get("nonce"),
                "prompt_message_id": pending.get("prompt_message_id"),
            }

        if snapshot is None:
            return

        logging.info(
            f"[YD-PROMPT] ⏰ Промпт {task_id} не подтверждён за {timeout_sec}s"
        )

        chat_id = snapshot["chat_id"]
        bot: Optional[Bot] = snapshot["bot"]
        owner_user_id = snapshot["owner_user_id"]
        session_key = snapshot["session_key"]
        task_dir = snapshot["task_dir"]
        nonce = snapshot["nonce"]
        prompt_message_id = snapshot["prompt_message_id"]

        if bot is not None and chat_id is not None:
            minutes = max(1, timeout_sec // 60)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⏰ <b>Время ожидания истекло</b> ({minutes} мин).\n\n"
                        f"Задача отменена, временные файлы удалены.\n"
                        f"Если хотите обработать файл — запустите /sunday заново."
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить о таймауте: {e}")

        if bot is not None and chat_id is not None and prompt_message_id is not None:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=prompt_message_id,
                    reply_markup=None,
                )
            except TelegramBadRequest:
                pass
            except Exception:
                pass

        await asyncio.shield(_yd_cleanup_task(
            task_id, session_key, task_dir,
            owner_user_id, chat_id, nonce,
        ))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.error(f"Ошибка watchdog {task_id}: {e}", exc_info=True)


# ==========================================
# ОЧИСТКА
# ==========================================

async def _yd_cleanup_task(
    task_id: str,
    session_key: str,
    task_dir: Path,
    owner_user_id: int,
    chat_id: int,
    nonce: str,
    bot: Optional[Bot] = None,
    status_msg=None,
    error: Optional[Exception] = None,
):
    """Идемпотентная очистка."""
    async with yd_session_lock:
        if task_id in _cleaning_tasks:
            logging.info(
                f"[YD-CLEANUP] task_id={task_id} уже чистится — skip"
            )
            return
        _cleaning_tasks.add(task_id)

    try:
        logging.info(
            f"[YD-CLEANUP] task_id={task_id}, session_key={session_key!r}, "
            f"reason={'error' if error else 'normal'}"
        )

        try:
            await _yd_stop_spinner(task_id)
        except Exception as e:
            logging.debug(f"[YD-CLEANUP] _yd_stop_spinner: {e}")

        # Снимаем клавиатуру
        try:
            session = sessions.get(task_id)
            if session is not None:
                pending = session.get("pending")
                if isinstance(pending, dict):
                    task_bot = pending.get("bot")
                    task_chat_id = pending.get("chat_id")
                    if task_bot is not None and task_chat_id is not None:
                        message_ids = []
                        for key in ("prompt_message_id", "status_message_id"):
                            mid = pending.get(key)
                            if mid is not None and mid not in message_ids:
                                message_ids.append(mid)

                        for mid in message_ids:
                            try:
                                await task_bot.edit_message_reply_markup(
                                    chat_id=task_chat_id,
                                    message_id=mid,
                                    reply_markup=None,
                                )
                            except TelegramBadRequest as e:
                                if "message is not modified" in str(e):
                                    logging.debug(
                                        f"[YD-CLEANUP] {mid}: клавиатура уже снята"
                                    )
                                else:
                                    logging.debug(
                                        f"[YD-CLEANUP] TelegramBadRequest "
                                        f"с {mid}: {e}"
                                    )
                            except Exception as e:
                                logging.debug(
                                    f"[YD-CLEANUP] Не удалось снять "
                                    f"клавиатуру с {mid}: {e}"
                                )
        except Exception as e:
            logging.debug(f"[YD-CLEANUP] Ошибка снятия клавиатуры: {e}")

        in_flight_ops = _active_ops.get(task_id)
        if in_flight_ops:
            ops_snapshot = list(in_flight_ops)
            logging.info(
                f"[YD-CLEANUP] {len(ops_snapshot)} in-flight ops для {task_id} — "
                f"откладываем удаление task_dir"
            )
            _spawn_deferred_cleanup(task_dir, ops_snapshot)
        else:
            _safe_delete_task_dir(task_dir)

        # Watchdog
        try:
            session = sessions.get(task_id)
            if session is not None:
                pending = session.get("pending")
                if isinstance(pending, dict):
                    timeout_task = pending.get("prompt_timeout_task")
                    current_task = asyncio.current_task()
                    if (
                        timeout_task is not None
                        and not timeout_task.done()
                        and timeout_task is not current_task
                    ):
                        timeout_task.cancel()
                    pending["prompt_timeout_task"] = None
                    pending["prompt_watchdog_nonce"] = None
                    pending["awaiting_range_for_idx"] = None
                    pending["prompt_nonce"] = None
                    pending["prompt_idx"] = None
        except Exception as e:
            logging.error(
                f"Ошибка отмены watchdog для {task_id}: {e}", exc_info=True
            )

        # Сессии
        try:
            async with yd_session_lock:
                picker = sessions.get(session_key)
                if picker is not None and picker.get("nonce") == nonce:
                    picker["processing"] = False
                    task_ids = picker.get("task_ids")
                    if isinstance(task_ids, list) and task_id in task_ids:
                        task_ids.remove(task_id)
                    sessions.pop(session_key, None)
                elif picker is not None:
                    task_ids = picker.get("task_ids")
                    if isinstance(task_ids, list) and task_id in task_ids:
                        task_ids.remove(task_id)
                        logging.warning(
                            f"[YD-CLEANUP] task_id={task_id} оказался в чужом "
                            f"picker'е — удаляем"
                        )

                sessions.pop(task_id, None)
                yd_active_tasks.discard(task_id)
        except Exception as e:
            logging.error(
                f"Ошибка очистки сессий для {task_id}: {e}", exc_info=True
            )

        _active_ops.pop(task_id, None)

        try:
            await asyncio.shield(yd_release(owner_user_id, chat_id, nonce))
        except Exception as e:
            logging.error(f"Ошибка yd_release для {task_id}: {e}", exc_info=True)

        if error is not None and bot is not None and status_msg is not None:
            try:
                await status_msg.edit_text(
                    f"❌ <b>Ошибка обработки</b>\n\n"
                    f"<code>{html_module.escape(str(error)[:200])}</code>\n\n"
                    f"Временные файлы удалены. Попробуйте снова.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logging.error(
                    f"Ошибка отправки сообщения для {task_id}: {e}"
                )
    finally:
        async with yd_session_lock:
            _cleaning_tasks.discard(task_id)


# ==========================================
# КОНВЕРТАЦИЯ + UPLOAD (v4.0 — N+1 ZIP)
# ==========================================

async def _yd_convert_and_upload_impl(
    bot: Bot,
    task_id: str,
    status_msg,
):
    """
    Вторая фаза v4.0: конвертация PNG + N+1 ZIP + upload.
    Для каждой выбранной категории — свой ZIP в свою папку.
    Для «остальных» — ZIP в pptx2png/{file_slug}/.
    """
    session = sessions.get(task_id)
    if not session or "pending" not in session:
        return

    def _is_cancelled() -> bool:
        s = sessions.get(task_id)
        return s is None or s.get("cancelled", False)

    if _is_cancelled():
        logging.info(f"[YD-UP] Задача {task_id} отменена — upload пропущен")
        pending = session.get("pending")
        if isinstance(pending, dict):
            await asyncio.shield(_yd_cleanup_task(
                task_id,
                pending.get("session_key"),
                pending.get("task_dir"),
                pending.get("owner_user_id"),
                pending.get("chat_id"),
                pending.get("nonce"),
            ))
        return

    pending = session["pending"]
    if not isinstance(pending, dict):
        return

    prepared = pending["prepared"]
    target_base = pending["target_base"]
    task_dir = pending["task_dir"]
    owner_user_id = pending["owner_user_id"]
    chat_id = pending["chat_id"]
    session_key = pending["session_key"]
    nonce = pending["nonce"]
    quality = pending.get("quality", "2k")

    if status_msg is not None and hasattr(status_msg, "message_id"):
        pending["status_message_id"] = status_msg.message_id

    zip_tmp_dir: Optional[Path] = None
    cleanup_done = False

    logging.info(
        f"[YD-UP] Старт v4.0: task_id={task_id}, файлов={len(prepared)}, "
        f"target_base={target_base!r}"
    )

    try:
        zip_tmp_dir = Path(tempfile.mkdtemp(prefix=f"pptx2png_{task_id}_"))
        logging.debug(f"[YD-UP] Создана временная папка {zip_tmp_dir}")

        total_uploaded_zip = 0
        total_slides_packed = 0
        total_failed = 0
        report_lines = [f"📁 Обработано файлов: <b>{len(prepared)}</b>\n"]
        links_by_folder: dict[str, str] = {}
        meta = _cat_meta()

        for f_idx, item in enumerate(prepared, start=1):
            _touch_task(task_dir)
            # Совместимость — синхронизируем плоские поля с sermon
            _normalize_item_ranges(item)

            if _is_cancelled():
                logging.info(f"[YD-UP] Отмена перед файлом #{f_idx}")
                return

            file_name = item["file_name"]
            file_name_esc = html_module.escape(file_name)
            file_slug = item["file_slug"]

            if item.get("failed_at_stage"):
                stage = item["failed_at_stage"]
                stage_text = {
                    "download":  "ошибка скачивания",
                    "normalize": "ошибка подготовки .ppt → .pptx",
                    "count":     "не удалось определить число слайдов",
                    "convert":   "ошибка конвертации",
                    "no_pngs":   "нет PNG",
                }.get(stage, stage)
                report_lines.append(f"❌ {file_name_esc} — {stage_text}")
                total_failed += 1
                continue

            file_path = item.get("file_path")
            if not file_path or not Path(file_path).exists():
                logging.error(f"[YD-UP] {file_name}: PPTX не найден")
                report_lines.append(f"❌ {file_name_esc} — файл PPTX не найден")
                total_failed += 1
                continue

            selected = set(item.get("selected_categories") or set())
            if not selected:
                logging.warning(
                    f"[YD-UP] {file_name}: ничего не выбрано — пропускаем"
                )
                report_lines.append(
                    f"⏭ {file_name_esc} — ничего не выбрано"
                )
                continue

            # Формируем шапку статуса
            selected_labels = []
            for cat in _cat_order():
                if cat in selected:
                    cm = meta.get(cat, {})
                    selected_labels.append(
                        f"{cm.get('emoji','❓')} {cm.get('name',cat)}"
                    )
            if "other" in selected:
                selected_labels.append("📄 Остальные")
            label_line = " + ".join(selected_labels) if selected_labels else "—"

            logging.info(
                f"[YD-UP] Файл #{f_idx}: {file_name!r}, "
                f"selected={sorted(selected)}"
            )

            header_lines = [
                f"🎬 <b>Выбранные категории:</b> {label_line}",
                f"📄 Файл: <code>{file_name_esc}</code>",
                "",
            ]
            header = "\n".join(header_lines)

            base_convert_text = f"{header}⚙️ Конвертирую в PNG"

            await _safe_edit(
                status_msg,
                base_convert_text,
                parse_mode="HTML",
                reply_markup=_get_cancel_keyboard(task_id).as_markup(),
            )

            temp_png_dir = task_dir / f"png_{f_idx}"
            temp_png_dir.mkdir(exist_ok=True)

            # === Конвертация PNG ===
            try:
                pngs, used_pptx = await _yd_with_spinner(
                    status_msg,
                    task_id,
                    base_convert_text,
                    _yd_run_protected(
                        task_id,
                        convert_all_pngs(
                            Path(file_path), temp_png_dir, quality
                        ),
                    ),
                )
            except Exception as e:
                logging.error(
                    f"[YD-UP] {file_name}: ошибка конвертации: {e}",
                    exc_info=True,
                )
                report_lines.append(
                    f"❌ {file_name_esc} — ошибка конвертации"
                )
                total_failed += 1
                continue

            if not pngs:
                logging.warning(f"[YD-UP] {file_name}: не создано PNG")
                report_lines.append(f"❌ {file_name_esc} — нет PNG")
                total_failed += 1
                continue

            pngs_sorted = sorted(pngs, key=lambda p: p.name)

            if _is_cancelled():
                logging.info(f"[YD-UP] Отмена после конвертации {file_name}")
                return

            # === Классификация по категориям (приоритет opening>prayer>sermon) ===
            pngs_by_cat: Dict[str, List[Path]] = {
                cat: [] for cat in _cat_order()
            }
            pngs_by_cat["other"] = []

            for slide_idx, png_path in enumerate(pngs_sorted, start=1):
                cat = _classify_slide(item, slide_idx)
                if cat is None:
                    pngs_by_cat["other"].append(png_path)
                else:
                    pngs_by_cat[cat].append(png_path)

            logging.debug(
                f"[YD-UP] {file_name}: классификация "
                + ", ".join(
                    f"{c}={len(pngs_by_cat[c])}"
                    for c in list(_cat_order()) + ["other"]
                )
            )

            # === Подготовка папок ===
            pptx2png_dir = (
                f"{target_base}/{yandex_state.config.pptx2png_folder}/{file_slug}"
            )

            need_other = (
                "other" in selected and pngs_by_cat["other"]
            )

            if need_other:
                ok0 = await _yd_async_protected(
                    task_id,
                    yandex_state.config.client.ensure_folder(pptx2png_dir),
                )
                if not ok0:
                    logging.error(
                        f"[YD-UP] {file_name}: не удалось создать {pptx2png_dir!r}"
                    )
                    report_lines.append(
                        f"❌ {file_name_esc} — не удалось создать папку остальных"
                    )
                    total_failed += 1
                    continue

            cat_folders_ok = {}
            for cat in _cat_order():
                if cat not in selected or not pngs_by_cat[cat]:
                    continue
                folder = _cat_folder_path(cat, target_base)
                ok = await _yd_async_protected(
                    task_id,
                    yandex_state.config.client.ensure_folder(folder),
                )
                cat_folders_ok[cat] = ok
                if not ok:
                    logging.error(
                        f"[YD-UP] {file_name}: не удалось создать {folder!r}"
                    )

            # === Формирование и загрузка ZIP по категориям ===
            entry_lines = [f"{f_idx}. 📄 <b>{file_name_esc}</b>"]
            file_uploaded = 0
            file_slides = 0

            for cat in _cat_order():
                if cat not in selected:
                    continue
                pngs_list = pngs_by_cat[cat]
                if not pngs_list:
                    continue
                if not cat_folders_ok.get(cat):
                    entry_lines.append(
                        f"   • ❌ {meta.get(cat,{}).get('emoji','❓')} "
                        f"{meta.get(cat,{}).get('name',cat)}: "
                        f"папка не создана"
                    )
                    total_failed += 1
                    continue

                if _is_cancelled():
                    logging.info(f"[YD-UP] Отмена перед ZIP {cat}")
                    return

                cat_meta = meta.get(cat, {})
                emoji = cat_meta.get("emoji", "❓")
                name = cat_meta.get("name", cat)
                zip_suffix = cat_meta.get("zip_suffix", cat)
                folder_path = _cat_folder_path(cat, target_base)
                cat_data = item.get("categories", {}).get(cat) or {}
                ranges = cat_data.get("ranges") or []
                ranges_text = _format_ranges_text(ranges)

                zip_name = f"{file_slug}_{zip_suffix}.zip"
                zip_path = zip_tmp_dir / zip_name

                try:
                    await _yd_to_thread(
                        task_id,
                        create_zip_stream, pngs_list, zip_path,
                    )
                except Exception as e:
                    logging.error(
                        f"[YD-UP] {file_name}: ZIP {cat} упал: {e}",
                        exc_info=True,
                    )
                    entry_lines.append(
                        f"   • ❌ {emoji} {name}: ошибка упаковки"
                    )
                    total_failed += 1
                    continue

                if _is_cancelled():
                    _safe_unlink(zip_path)
                    return

                zip_size = zip_path.stat().st_size
                remote_path = f"{folder_path}/{zip_name}"

                base_upload_text = (
                    f"{header}"
                    f"📤 Загружаю {emoji} {name} "
                    f"(<code>{file_name_esc}</code>)"
                )

                await _safe_edit(
                    status_msg,
                    base_upload_text,
                    parse_mode="HTML",
                    reply_markup=_get_cancel_keyboard(task_id).as_markup(),
                )

                if _is_cancelled():
                    _safe_unlink(zip_path)
                    return

                ok = await _yd_with_spinner(
                    status_msg,
                    task_id,
                    base_upload_text,
                    _yd_async_protected(
                        task_id,
                        yandex_state.config.client.upload_file(
                            zip_path, remote_path
                        ),
                    ),
                )

                if ok:
                    total_uploaded_zip += 1
                    total_slides_packed += len(pngs_list)
                    file_uploaded += 1
                    file_slides += len(pngs_list)
                    links_by_folder[folder_path] = f"{emoji} {name}"
                    folder_short = folder_path.rsplit("/", 1)[-1]
                    entry_lines.append(
                        f"   • {emoji} {name} ({ranges_text}): "
                        f"{len(pngs_list)} с. → "
                        f"<code>{html_module.escape(folder_short)}/"
                        f"{html_module.escape(zip_name)}</code> "
                        f"({_format_size(zip_size)})"
                    )
                    logging.info(
                        f"[YD-UP] {file_name}: {cat} ZIP загружен "
                        f"({len(pngs_list)} с.)"
                    )
                else:
                    total_failed += 1
                    entry_lines.append(
                        f"   • ❌ {emoji} {name}: не удалось загрузить"
                    )
                    logging.error(f"[YD-UP] {file_name}: {cat} ZIP failed")

                _safe_unlink(zip_path)
                for png in pngs_list:
                    _safe_unlink(png)

            # === Остальные ===
            if need_other:
                pngs_list = pngs_by_cat["other"]
                if _is_cancelled():
                    logging.info(f"[YD-UP] Отмена перед ZIP other")
                    return

                zip_name = f"{file_slug}_слайды.zip"
                zip_path = zip_tmp_dir / zip_name

                try:
                    await _yd_to_thread(
                        task_id,
                        create_zip_stream, pngs_list, zip_path,
                    )
                except Exception as e:
                    logging.error(
                        f"[YD-UP] {file_name}: ZIP other упал: {e}",
                        exc_info=True,
                    )
                    entry_lines.append(
                        "   • ❌ 📄 Остальные: ошибка упаковки"
                    )
                    total_failed += 1
                else:
                    if _is_cancelled():
                        _safe_unlink(zip_path)
                        return

                    zip_size = zip_path.stat().st_size
                    remote_path = f"{pptx2png_dir}/{zip_name}"

                    base_upload_text = (
                        f"{header}"
                        f"📤 Загружаю 📄 Остальные "
                        f"(<code>{file_name_esc}</code>)"
                    )

                    await _safe_edit(
                        status_msg,
                        base_upload_text,
                        parse_mode="HTML",
                        reply_markup=_get_cancel_keyboard(task_id).as_markup(),
                    )

                    if _is_cancelled():
                        _safe_unlink(zip_path)
                        return

                    ok = await _yd_with_spinner(
                        status_msg,
                        task_id,
                        base_upload_text,
                        _yd_async_protected(
                            task_id,
                            yandex_state.config.client.upload_file(
                                zip_path, remote_path
                            ),
                        ),
                    )

                    folder_short = (
                        f"{yandex_state.config.pptx2png_folder}/{file_slug}"
                    )

                    if ok:
                        total_uploaded_zip += 1
                        total_slides_packed += len(pngs_list)
                        file_uploaded += 1
                        file_slides += len(pngs_list)
                        links_by_folder[pptx2png_dir] = "📄 Остальные"
                        entry_lines.append(
                            f"   • 📄 Остальные: {len(pngs_list)} с. → "
                            f"<code>{html_module.escape(folder_short)}/"
                            f"{html_module.escape(zip_name)}</code> "
                            f"({_format_size(zip_size)})"
                        )
                        logging.info(
                            f"[YD-UP] {file_name}: other ZIP загружен "
                            f"({len(pngs_list)} с.)"
                        )
                    else:
                        total_failed += 1
                        entry_lines.append(
                            "   • ❌ 📄 Остальные: не удалось загрузить"
                        )

                    _safe_unlink(zip_path)
                    for png in pngs_list:
                        _safe_unlink(png)

            # Если ничего не загружено
            if file_uploaded == 0:
                entry_lines.append("   • ℹ️ Ничего не загружено")

            incomplete_warning = item.get("incomplete_warning")
            if incomplete_warning:
                entry_lines.append(f"   • {incomplete_warning}")

            report_lines.append("\n".join(entry_lines))

        if _is_cancelled():
            logging.info(f"[YD-UP] Отмена перед отправкой отчёта")
            return

        if total_failed > 0:
            report_lines.append(
                f"\n⚠️ Всего загружено архивов: <b>{total_uploaded_zip}</b>\n"
                f"📊 Всего слайдов: <b>{total_slides_packed}</b>\n"
                f"❌ Ошибок: <b>{total_failed}</b>"
            )
        else:
            report_lines.append(
                f"\n📊 Всего загружено архивов: <b>{total_uploaded_zip}</b>\n"
                f"📊 Всего слайдов: <b>{total_slides_packed}</b>"
            )

        if links_by_folder:
            report_lines.append("\n🔗 <b>Ссылки на Яндекс.Диск:</b>")
            for folder_path, label in links_by_folder.items():
                url = _yd_public_url(folder_path)
                folder_name = folder_path.rsplit("/", 1)[-1]
                report_lines.append(
                    f'   • {label}: <a href="{url}">'
                    f'{html_module.escape(folder_name)}/</a>'
                )

        try:
            await status_msg.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        except Exception:
            pass

        await _yd_send_report(
            bot=bot,
            chat_id=chat_id,
            status_msg=status_msg,
            report_lines=report_lines,
            total_uploaded=total_uploaded_zip,
            total_failed=total_failed,
        )

        logging.info(
            f"[YD-UP] Итог v4.0: zip={total_uploaded_zip}, "
            f"slides={total_slides_packed}, failed={total_failed}"
        )

    except asyncio.CancelledError:
        logging.info(f"[YD-UP] Worker {task_id} отменён — выполняю cleanup")
        cleanup_done = True
        try:
            await asyncio.shield(_yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
            ))
        except Exception as e:
            logging.error(
                f"[YD-UP] cleanup после отмены упал: {e}", exc_info=True
            )
        raise

    except Exception as e:
        logging.error(f"[YD-UP] Ошибка: {e}", exc_info=True)
        cleanup_done = True
        await _yd_cleanup_task(
            task_id, session_key, task_dir,
            owner_user_id, chat_id, nonce,
            bot=bot, status_msg=status_msg, error=e,
        )
        return
    finally:
        if zip_tmp_dir is not None and zip_tmp_dir.exists():
            try:
                shutil.rmtree(zip_tmp_dir)
                logging.debug(f"[YD-UP] Удалена временная папка {zip_tmp_dir}")
            except Exception as e:
                logging.warning(
                    f"[YD-UP] Не удалось удалить {zip_tmp_dir}: {e}",
                    exc_info=True,
                )

        if not cleanup_done:
            await _yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
            )


async def _yd_convert_and_upload(
    bot: Bot,
    task_id: str,
    status_msg,
) -> asyncio.Task:
    """Запускает _yd_convert_and_upload_impl как detached worker."""
    return await _run_worker_detached(
        task_id,
        lambda: _yd_convert_and_upload_impl(
            bot=bot, task_id=task_id, status_msg=status_msg,
        ),
        name="convert",
    )


async def _yd_send_report(
    bot: Bot,
    chat_id: int,
    status_msg,
    report_lines: list,
    total_uploaded: int,
    total_failed: int,
):
    MAX_MSG_LEN = 3500
    chunks = []
    current_chunk = []
    current_len = 0

    for line in report_lines:
        line_len = len(line) + 1
        if current_len + line_len > MAX_MSG_LEN and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    delivered = 0
    failed_chunks = []
    first_delivered = False

    if chunks:
        try:
            await status_msg.edit_text(
                chunks[0], parse_mode="HTML", disable_web_page_preview=True
            )
            delivered += 1
            first_delivered = True
        except Exception as e:
            logging.error(
                f"Ошибка edit_text первой части: {e}", exc_info=True
            )
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunks[0],
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                delivered += 1
                first_delivered = True
            except Exception as e2:
                logging.error(
                    f"Не удалось отправить первую часть: {e2}", exc_info=True
                )
                failed_chunks.append(1)

    if not first_delivered:
        try:
            fallback = (
                f"⚠️ Не удалось показать полный отчёт.\n"
                f"📊 Загружено: {total_uploaded}"
            )
            if total_failed:
                fallback += f"\n❌ Ошибок: {total_failed}"
            await bot.send_message(chat_id=chat_id, text=fallback)
        except Exception as e:
            logging.error(
                f"Не удалось отправить fallback-отчёт: {e}", exc_info=True
            )

    for i, chunk in enumerate(chunks[1:], start=2):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            delivered += 1
        except Exception as e:
            logging.error(f"Ошибка отправки части {i}: {e}", exc_info=True)
            failed_chunks.append(i)

    if failed_chunks and delivered > 0:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Не удалось доставить {len(failed_chunks)} "
                    f"из {len(chunks)} частей отчёта. Проверьте Яндекс.Диск."
                ),
            )
        except Exception as e:
            logging.error(f"Не удалось отправить предупреждение: {e}")


# ==========================================
# ХЕНДЛЕРЫ
# ==========================================

@router.message(Command("sunday"))
async def cmd_sunday(message: types.Message, check_access, bot: Bot):
    if not await check_access(message):
        return

    if yandex_state.config.client is None:
        await message.reply(
            "❌ Яндекс.Диск не настроен. Обратитесь к администратору."
        )
        return

    if not yandex_state.config.base_path:
        await message.reply(
            "❌ Не задан base_path Яндекс.Диска в settings.ini."
        )
        return

    logging.info(
        f"[YD] /sunday от user={message.from_user.id}, "
        f"base_path={yandex_state.config.base_path!r}"
    )

    session_key = f"yd_{message.from_user.id}_{message.chat.id}"

    async with yd_session_lock:
        existing = sessions.get(session_key)
        status = _picker_status(existing)

        if status == "draft":
            await message.reply(
                "⏳ <b>Проверяю Яндекс.Диск, подождите…</b>\n\n"
                "Первая команда ещё выполняется. "
                "Список файлов появится через несколько секунд.",
                parse_mode="HTML",
            )
            return

        if status == "working":
            await message.reply(
                "⚠️ <b>У вас уже есть активная задача.</b>\n\n"
                "Дождитесь её завершения или отмените командой /cancel_yd, "
                "затем запустите /sunday снова.",
                parse_mode="HTML",
            )
            return

        draft_id = secrets.token_hex(4)
        sessions[session_key] = {
            "user_id": message.from_user.id,
            "chat_id": message.chat.id,
            "processing": False,
            "_draft": True,
            "_draft_id": draft_id,
            "task_ids": [],
            "cancelled": False,
            "created_at": time.time(),
        }

    nonce = None
    session_created = False

    try:
        nonce = await yd_try_acquire(message.from_user.id, message.chat.id)
        if nonce is None:
            await message.reply(
                "⚠️ <b>У вас уже есть активная задача.</b>\n\n"
                "Дождитесь её завершения или отмените командой /cancel_yd, "
                "затем запустите /sunday снова.",
                parse_mode="HTML",
            )
            return

        status_msg = await message.reply("🔍 Проверяю Яндекс.Диск...")

        ok, err = await yandex_state.config.client.check_access()
        if not ok:
            await status_msg.edit_text(
                f"❌ <b>Яндекс.Диск недоступен</b>\n\n"
                f"Причина: <code>{html_module.escape(str(err))}</code>\n\n"
                f"Проверьте токен в <code>config.ini</code>.",
                parse_mode="HTML",
            )
            return

        sunday = get_nearest_sunday()
        sunday_str = sunday.strftime("%d.%m.%Y")
        month_str = month_folder_name(sunday)

        await status_msg.edit_text(
            f"✅ Яндекс.Диск доступен\n"
            f"📅 Ближайшее воскресенье: "
            f"<b>{html_module.escape(sunday_str)}</b>\n"
            f"📁 Ожидаемая папка: "
            f"<code>{html_module.escape(f'{month_str}/{sunday_str}')}</code>\n\n"
            f"🔍 Проверяю структуру папок...",
            parse_mode="HTML",
        )

        paths = await resolve_sunday_paths(
            yandex_state.config.client,
            yandex_state.config.base_path,
            sunday,
            yandex_state.config.source_folder,
            yandex_state.config.target_folder,
        )
        if not paths:
            src_esc = html_module.escape(yandex_state.config.source_folder)
            tgt_esc = html_module.escape(yandex_state.config.target_folder)
            await status_msg.edit_text(
                f"❌ <b>Структура папок не найдена</b>\n\n"
                f"Ожидалось:\n"
                f"<code>{html_module.escape(yandex_state.config.base_path)}/</code>\n"
                f"<code>  {html_module.escape(month_str)}/</code>\n"
                f"<code>    {html_module.escape(sunday_str)}/</code>\n"
                f"<code>      {src_esc}/</code>\n"
                f"<code>      {tgt_esc}/</code>",
                parse_mode="HTML",
            )
            return

        try:
            pptx_files = await find_pptx_in_source(
                yandex_state.config.client, paths["source"], sunday
            )
        except YandexDiskError as e:
            logging.error(f"[YD] Ошибка доступа к источнику: {e}")
            await status_msg.edit_text(
                f"❌ <b>Ошибка обращения к Яндекс.Диску</b>\n\n"
                f"<code>{html_module.escape(str(e))}</code>\n\n"
                f"Попробуйте позже.",
                parse_mode="HTML",
            )
            return

        logging.info(f"[YD] Найдено pptx: {len(pptx_files)}")

        if not pptx_files:
            src_esc = html_module.escape(yandex_state.config.source_folder)
            await status_msg.edit_text(
                f"📅 Ближайшее воскресенье: "
                f"<b>{html_module.escape(sunday_str)}</b>\n"
                f"📍 Папка: "
                f"<code>{html_module.escape(paths['source'])}</code>\n\n"
                f"❌ <b>pptx-файлы не найдены.</b>\n\n"
                f"Положите pptx с датой <code>{sunday:%d.%m.%y}</code> "
                f"в папку <code>{src_esc}</code> и попробуйте снова.",
                parse_mode="HTML",
            )
            return

        MAX_LEN = 3500
        header_lines = [
            f"📅 Ближайшее воскресенье: "
            f"<b>{html_module.escape(sunday_str)}</b>",
            f"📍 Папка: <code>{html_module.escape(paths['source'])}</code>",
            "",
            f"📄 <b>Найдено файлов: {len(pptx_files)}</b>",
            "",
        ]
        body_lines = []
        omitted = 0
        for idx, f in enumerate(pptx_files, start=1):
            size_mb = f.get("size", 0) / (1024 * 1024)
            name_escaped = html_module.escape(f["name"])
            line = f"{idx}. <code>{name_escaped}</code> — {size_mb:.1f} МБ"
            candidate = "\n".join(header_lines + body_lines + [line])
            if len(candidate) > MAX_LEN:
                omitted = len(pptx_files) - idx + 1
                break
            body_lines.append(line)

        if omitted > 0:
            body_lines.append("")
            body_lines.append(
                f"…и ещё <b>{omitted}</b> файл(ов) не показано."
            )

        body_lines.append("")
        body_lines.append("🎬 Выберите файл для обработки:")

        if not await yd_is_active(
            message.from_user.id, message.chat.id, nonce
        ):
            logging.info(
                f"[YD] Сессия {message.from_user.id}:{message.chat.id} "
                f"была отменена во время выполнения"
            )
            try:
                await status_msg.edit_text("❌ Операция отменена пользователем.")
            except Exception:
                pass
            return

        race_detected = False
        async with yd_session_lock:
            old_picker = sessions.get(session_key)
            old_status = _picker_status(old_picker)

            if old_status == "working":
                logging.warning(
                    f"[YD] Race: picker {session_key!r} в состоянии "
                    f"'working' — отказ"
                )
                race_detected = True
            elif old_status == "draft":
                if old_picker.get("_draft_id") != draft_id:
                    logging.warning(
                        f"[YD] Race: picker {session_key!r} — чужой draft, отказ"
                    )
                    race_detected = True

            if not race_detected:
                sessions[session_key] = {
                    "user_id": message.from_user.id,
                    "chat_id": message.chat.id,
                    "sunday": sunday,
                    "sunday_str": sunday_str,
                    "paths": paths,
                    "files": pptx_files,
                    "nonce": nonce,
                    "created_at": time.time(),
                    "task_ids": [],
                    "cancelled": False,
                    "processing": False,
                }

        if race_detected:
            try:
                await status_msg.edit_text(
                    "⚠️ <b>Другая команда /sunday уже активна.</b>\n\n"
                    "Дождитесь её завершения или отмените /cancel_yd.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        kb = InlineKeyboardBuilder()
        for idx, f in enumerate(pptx_files):
            prefix = "🎯" if "служение" in f["name"].lower() else "📄"
            kb.row(InlineKeyboardButton(
                text=f"{prefix} {f['name']}",
                callback_data=(
                    f"yd_pick:{message.from_user.id}:{nonce}:{idx}"
                ),
            ))
        if len(pptx_files) > 1:
            kb.row(InlineKeyboardButton(
                text="📁 Все подряд",
                callback_data=(
                    f"yd_pick:{message.from_user.id}:{nonce}:all"
                ),
            ))
        kb.row(InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=f"yd_cancel:{message.from_user.id}:{nonce}",
        ))

        await status_msg.edit_text(
            "\n".join(header_lines + body_lines),
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        session_created = True

        await yd_release(message.from_user.id, message.chat.id, nonce)
        logging.info(
            f"[YD] 🔓 Сессия {message.from_user.id}:{message.chat.id} "
            f"освобождена после показа списка"
        )

    except Exception as e:
        logging.error(f"[YD] Ошибка cmd_sunday: {e}", exc_info=True)
        try:
            await message.reply(f"❌ Ошибка: {str(e)[:200]}")
        except Exception:
            pass
    finally:
        async with yd_session_lock:
            existing = sessions.get(session_key)
            if (
                existing is not None
                and existing.get("_draft")
                and existing.get("_draft_id") == draft_id
            ):
                sessions.pop(session_key, None)

        if nonce is not None and not session_created:
            released = await yd_release(
                message.from_user.id, message.chat.id, nonce
            )
            if released:
                logging.info(
                    f"[YD] 🔓 Сессия {message.from_user.id}:{message.chat.id} "
                    f"освобождена (неудачный запуск)"
                )


@router.callback_query(F.data.startswith("yd_pick:"))
async def yd_pick(
    callback: types.CallbackQuery,
    bot: Bot,
    SHM_DIR: str,
    user_mgr,
):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return

    try:
        owner_user_id = int(parts[1])
    except ValueError:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return

    callback_nonce = parts[2]
    file_selector = parts[3]

    if callback.from_user.id != owner_user_id:
        await _safe_answer(
            callback,
            "❌ Только автор запроса может выбрать файл.",
            show_alert=True,
        )
        return

    session_key = f"yd_{owner_user_id}_{callback.message.chat.id}"

    async with yd_session_lock:
        session = sessions.get(session_key)
        if not session or session.get("nonce") != callback_nonce:
            await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
            return

        if _picker_is_active(session):
            await _safe_answer(callback, "⏳ Обработка уже запущена.", show_alert=True)
            return

        session["processing"] = True
        files = session["files"]
        sunday = session["sunday"]
        paths = session["paths"]

        if file_selector == "all":
            files_to_process = files
        else:
            try:
                idx = int(file_selector)
                if idx < 0 or idx >= len(files):
                    session.pop("processing", None)
                    await _safe_answer(callback, "❌ Файл не найден.", show_alert=True)
                    return
                files_to_process = [files[idx]]
            except ValueError:
                session.pop("processing", None)
                await _safe_answer(
                    callback, "❌ Некорректный выбор.", show_alert=True
                )
                return

    if file_selector == "all":
        await _safe_answer(callback, "⏳ Обрабатываю все файлы...")
    else:
        await _safe_answer(callback, "⏳ Начинаю обработку...")

    try:
        await _yd_prepare_files(
            callback=callback,
            bot=bot,
            SHM_DIR=SHM_DIR,
            user_mgr=user_mgr,
            files_to_process=files_to_process,
            sunday=sunday,
            paths=paths,
            session_key=session_key,
            nonce=callback_nonce,
        )
    except Exception as e:
        logging.error(
            f"[YD-PICK] ошибка запуска подготовки: {e}", exc_info=True,
        )
        try:
            async with yd_session_lock:
                picker = sessions.get(session_key)
                if (
                    picker is not None
                    and picker.get("nonce") == callback_nonce
                ):
                    picker["processing"] = False
        except Exception as cleanup_err:
            logging.error(
                f"[YD-PICK] не удалось сбросить processing: {cleanup_err}",
                exc_info=True,
            )


@router.callback_query(F.data.startswith("yd_cancel:"))
async def yd_cancel_callback(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return

    try:
        owner_user_id = int(parts[1])
    except ValueError:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return

    callback_nonce = parts[2]

    if callback.from_user.id != owner_user_id:
        await _safe_answer(
            callback,
            "❌ Только автор запроса может отменить операцию.",
            show_alert=True,
        )
        return

    session_key = f"yd_{owner_user_id}_{callback.message.chat.id}"

    is_stale = False
    task_ids_to_cancel = []
    async with yd_session_lock:
        session = sessions.get(session_key)
        if session is None or session.get("nonce") != callback_nonce:
            is_stale = True
        else:
            session["cancelled"] = True
            task_ids_to_cancel = list(session.get("task_ids", []))
            sessions.pop(session_key, None)

    if is_stale:
        await yd_release(
            owner_user_id, callback.message.chat.id, callback_nonce
        )
        try:
            await callback.message.edit_text("❌ Сессия уже неактивна.")
        except Exception:
            pass
        await _safe_answer(callback, "❌ Сессия уже неактивна.", show_alert=True)
        return

    workers_to_cancel = []
    for tid in task_ids_to_cancel:
        async with yd_session_lock:
            task_sess = sessions.get(tid)
            if task_sess is not None:
                task_sess["cancelled"] = True
                w = task_sess.get("worker_task")
                if w is not None:
                    workers_to_cancel.append((tid, w))

    for tid, w in workers_to_cancel:
        if w is not None and not w.done() and w is not asyncio.current_task():
            _spawn_cancel_worker(w, tid)

    await yd_release(owner_user_id, callback.message.chat.id, callback_nonce)

    try:
        await callback.message.edit_text("❌ Операция отменена.")
    except Exception:
        pass
    await _safe_answer(callback)


@router.message(Command("cancel_yd"))
async def cmd_cancel_yd(message: types.Message, check_access):
    if not await check_access(message):
        return

    session_key = f"yd_{message.from_user.id}_{message.chat.id}"

    task_ids_to_cancel = []
    session = None
    async with yd_session_lock:
        session = sessions.get(session_key)
        if session is not None:
            session["cancelled"] = True
            task_ids_to_cancel = list(session.get("task_ids", []))
            sessions.pop(session_key, None)

    workers_to_cancel = []
    for tid in task_ids_to_cancel:
        async with yd_session_lock:
            task_sess = sessions.get(tid)
            if task_sess is not None:
                task_sess["cancelled"] = True
                w = task_sess.get("worker_task")
                if w is not None:
                    workers_to_cancel.append((tid, w))

    for tid, w in workers_to_cancel:
        if w is not None and not w.done() and w is not asyncio.current_task():
            _spawn_cancel_worker(w, tid)

    released = await yd_release(message.from_user.id, message.chat.id)

    if session is None and not task_ids_to_cancel and released:
        await message.reply("ℹ️ У вас нет активной сессии Яндекс.Диска.")
        return

    await message.reply("✅ Сессия Яндекс.Диска сброшена.")


# ==========================================
# НОВЫЕ ХЕНДЛЕРЫ v4.0 — toggle и convert
# ==========================================

@router.callback_query(F.data.startswith("yd_cat_toggle:"))
async def yd_cat_toggle(callback: types.CallbackQuery, bot: Bot):
    """Переключение галки категории."""
    parts = callback.data.split(":")
    if len(parts) != 5:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return

    task_id = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return
    nonce = parts[3]
    cat = parts[4]

    valid_cats = set(_cat_order()) | {"other"}
    if cat not in valid_cats:
        await _safe_answer(callback, "❌ Неизвестная категория.", show_alert=True)
        return

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return

    pending = session["pending"]
    if not isinstance(pending, dict):
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return

    if pending.get("prompt_nonce") != nonce or pending.get("prompt_idx") != idx:
        await _safe_answer(callback, "⏳ Промпт уже обработан.", show_alert=True)
        return

    if callback.from_user.id != pending["owner_user_id"]:
        await _safe_answer(callback, "❌ Только автор.", show_alert=True)
        return

    item = pending["prepared"][idx]
    selected = set(item.get("selected_categories") or set())

    if cat in selected:
        selected.discard(cat)
    else:
        selected.add(cat)

    item["selected_categories"] = selected

    # Перерисовываем промпт (без claim — nonce тот же)
    try:
        await callback.message.edit_text(
            _render_category_prompt_text(item, selected),
            parse_mode="HTML",
            reply_markup=_render_category_toggle_keyboard(
                task_id, idx, nonce, item, selected,
                item.get("total_slides", 0),
            ).as_markup(),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logging.warning(f"[YD-CAT-TOGGLE] edit_text: {e}")

    # Обновляем watchdog
    existing_task = pending.get("prompt_timeout_task")
    if existing_task is not None and not existing_task.done():
        existing_task.cancel()
    pending["prompt_timeout_task"] = asyncio.create_task(
        _yd_prompt_timeout_watchdog(
            task_id, yandex_state.config.prompt_timeout_sec, nonce
        )
    )
    pending["prompt_watchdog_nonce"] = nonce

    await _safe_answer(callback)


@router.callback_query(F.data.startswith("yd_cat_convert:"))
async def yd_cat_convert(callback: types.CallbackQuery, bot: Bot):
    """Запуск конвертации с выбранными категориями."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return

    task_id = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return
    nonce = parts[3]

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return

    claimed = await _yd_claim_prompt(callback)
    if claimed is None:
        return
    pending = claimed

    item = pending["prepared"][idx]
    selected = set(item.get("selected_categories") or set())

    if not selected:
        await _safe_answer(
            callback,
            "❌ Выберите хотя бы одну категорию.",
            show_alert=True,
        )
        # Возвращаем промпт (claim уже сбросил nonce — рендерим заново)
        await _yd_render_category_prompt(
            task_id=task_id, item=item, status_msg=callback.message,
        )
        return

    item["confirmed"] = True

    await _safe_answer(callback, "⏳ Принято, начинаю конвертацию...")

    # Остались ли неподтверждённые файлы?
    remaining = [
        p for p in pending["prepared"]
        if "file_path" in p and not p.get("confirmed")
    ]

    if remaining:
        await _yd_render_category_prompt(
            task_id=task_id,
            item=remaining[0],
            status_msg=callback.message,
        )
        return

    try:
        await _yd_convert_and_upload(
            bot=bot,
            task_id=task_id,
            status_msg=callback.message,
        )
    except Exception as e:
        logging.error(
            f"[YD-CAT-CONVERT] ошибка запуска: {e}", exc_info=True,
        )


@router.callback_query(F.data.startswith("yd_cat_edit:"))
async def yd_cat_edit(callback: types.CallbackQuery, bot: Bot):
    """
    v4.0: заглушка. Ручное редактирование диапазона по категориям — v4.1.
    Пока говорим пользователю «в разработке».
    """
    await _safe_answer(
        callback,
        "✏️ Ручное редактирование диапазона по категориям — в следующем "
        "обновлении. Пока используйте автоматические диапазоны.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("yd_cat_noop:"))
async def yd_cat_noop(callback: types.CallbackQuery):
    """Disabled-кнопки (не найдено / empty)."""
    parts = callback.data.split(":")
    reason = parts[-1] if len(parts) >= 1 else ""

    if reason == "empty":
        await _safe_answer(
            callback,
            "❌ Ничего не выбрано. Отметьте хотя бы одну категорию.",
            show_alert=True,
        )
    else:
        await _safe_answer(
            callback,
            "ℹ️ Категория не найдена в заметках.",
            show_alert=True,
        )


# ==========================================
# СТАРЫЕ ХЕНДЛЕРЫ (fallback)
# ==========================================

@router.callback_query(F.data.startswith("yd_sermon_mode:"))
async def yd_sermon_mode(callback: types.CallbackQuery, bot: Bot):
    """Старый хендлер выбора режима (fallback)."""
    parts = callback.data.split(":")
    if len(parts) != 5:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return

    task_id = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return
    nonce = parts[3]
    mode = parts[4]

    if mode not in ("sermon", "other", "both"):
        await _safe_answer(callback, "❌ Неизвестный режим.", show_alert=True)
        return

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return

    claimed = await _yd_claim_prompt(callback)
    if claimed is None:
        return
    pending = claimed

    item = pending["prepared"][idx]
    _normalize_item_ranges(item)

    if mode in ("sermon", "both") and (
        item.get("start") is None or item.get("end") is None
    ):
        await _safe_answer(
            callback,
            "❌ Диапазон не задан. Укажите его вручную.",
            show_alert=True,
        )
        await _yd_render_sermon_prompt(task_id, item, callback.message)
        return

    # Конвертируем старую mode → selected_categories
    if mode == "sermon":
        item["selected_categories"] = {"sermon"}
    elif mode == "other":
        item["selected_categories"] = {"other"}
    else:  # both
        item["selected_categories"] = {"sermon", "other"}

    item["confirmed"] = True
    item["convert_mode"] = mode

    await _safe_answer(callback, "⏳ Принято, начинаю конвертацию...")

    remaining = [
        p for p in pending["prepared"]
        if "file_path" in p and not p.get("confirmed")
    ]

    if remaining:
        await _yd_render_sermon_prompt(
            task_id, remaining[0], callback.message
        )
        return

    try:
        await _yd_convert_and_upload(
            bot=bot, task_id=task_id, status_msg=callback.message,
        )
    except Exception as e:
        logging.error(
            f"[YD-MODE] ошибка запуска конвертации: {e}", exc_info=True,
        )


@router.callback_query(F.data.startswith("yd_task_cancel:"))
async def yd_task_cancel_callback(callback: types.CallbackQuery):
    """Отмена текущей Yandex-задачи."""
    parts = callback.data.split(":")
    if len(parts) != 2:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return

    task_id = parts[1]

    await _safe_answer(callback, "❌ Отмена запрошена…")

    session = sessions.get(task_id)
    if not session:
        logging.info(
            f"[YD-TASK-CANCEL] Задача {task_id!r} не найдена (уже очищена)"
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    owner_user_id = session.get("user_id")
    if callback.from_user.id != owner_user_id:
        logging.info(
            f"[YD-TASK-CANCEL] Пользователь {callback.from_user.id} "
            f"не владелец задачи {task_id}"
        )
        return

    await _yd_stop_spinner(task_id)

    session["cancelled"] = True

    worker = session.get("worker_task")
    if (
        worker is not None
        and not worker.done()
        and worker is not asyncio.current_task()
    ):
        logging.info(
            f"[YD-TASK-CANCEL] Запускаем фоновую отмену worker {task_id}"
        )
        _spawn_cancel_worker(worker, task_id)

    owner_chat_id = session.get("chat_id")
    owner_nonce = session.get("nonce")
    if owner_chat_id is not None and owner_nonce:
        picker_key = f"yd_{owner_user_id}_{owner_chat_id}"
        async with yd_session_lock:
            has_live_siblings = _picker_has_live_tasks_locked(
                picker_key, exclude_task_id=task_id
            )
        if has_live_siblings:
            logging.info(
                f"[YD-TASK-CANCEL] Не освобождаем picker {picker_key}: "
                f"остались живые sibling-задачи"
            )
        else:
            try:
                released = await asyncio.shield(
                    yd_release(owner_user_id, owner_chat_id, owner_nonce)
                )
                logging.info(
                    f"[YD-TASK-CANCEL] yd_release для {task_id}: "
                    f"released={released}"
                )
            except Exception as e:
                logging.error(
                    f"[YD-TASK-CANCEL] yd_release упал для {task_id}: {e}",
                    exc_info=True,
                )

    pending = session.get("pending")

    if not isinstance(pending, dict):
        logging.info(
            f"[YD-TASK-CANCEL] Задача {task_id!r} отменена на стадии подготовки"
        )
        try:
            await callback.message.edit_text(
                "⏳ <b>Отмена запрошена…</b>\n\n"
                "Задача будет отменена после текущего шага.\n"
                "<i>Больше ничего нажимать не нужно.</i>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    logging.info(
        f"[YD-TASK-CANCEL] Пользователь {callback.from_user.id} "
        f"отменил задачу {task_id}"
    )

    timeout_task = pending.get("prompt_timeout_task")
    if timeout_task is not None and not timeout_task.done():
        timeout_task.cancel()
    pending["prompt_timeout_task"] = None
    pending["prompt_watchdog_nonce"] = None
    pending["awaiting_range_for_idx"] = None

    prompt_active = pending.get("prompt_nonce") is not None

    if prompt_active:
        try:
            await callback.message.edit_text(
                "❌ <b>Задача отменена</b>\n\n"
                "Временные файлы удалены.\n"
                "Запустите <code>/sunday</code> заново, если нужно.",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass

        await asyncio.shield(_yd_cleanup_task(
            task_id=task_id,
            session_key=pending.get("session_key"),
            task_dir=pending.get("task_dir"),
            owner_user_id=owner_user_id,
            chat_id=pending.get("chat_id"),
            nonce=pending.get("nonce"),
        ))
    else:
        try:
            await callback.message.edit_text(
                "⏳ <b>Отмена запрошена…</b>\n\n"
                "Текущий шаг завершится, затем задача будет очищена.\n"
                "<i>Больше ничего нажимать не нужно.</i>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("yd_sermon_edit:"))
async def yd_sermon_edit(callback: types.CallbackQuery, bot: Bot):
    """
    Старое ручное редактирование диапазона (только для sermon).
    Работает через handlers.py (текстовый ввод).
    """
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _safe_answer(callback, "❌ Некорректный запрос.", show_alert=True)
        return
    task_id = parts[1]

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return

    claimed = await _yd_claim_prompt(callback)
    if claimed is None:
        return
    pending = claimed

    idx = int(parts[2])
    manual_nonce = secrets.token_hex(4)

    current = pending["prepared"][idx]
    _normalize_item_ranges(current)
    file_name_esc = html_module.escape(current["file_name"])

    total_slides = current.get("total_slides", 0)
    if total_slides == 0:
        file_path = current.get("file_path")
        if file_path and Path(file_path).exists():
            try:
                from pptx import Presentation
                prs = Presentation(str(file_path))
                total_slides = len(prs.slides._sldIdLst)
            except Exception as e:
                logging.warning(f"Не удалось определить число слайдов: {e}")

    matches = current.get("matches", []) or []
    start = current.get("start")
    end = current.get("end")
    ranges = current.get("ranges")

    context_lines = []
    if matches:
        preview = ", ".join(str(n) for n in matches[:20])
        if len(matches) > 20:
            preview += f" …и ещё {len(matches) - 20}"
        context_lines.append(
            f"📌 <b>Найдены пометки на слайдах:</b> <code>{preview}</code>"
        )
        if start is not None and end is not None:
            ranges_text = _format_ranges_text(ranges, start, end)
            context_lines.append(
                f"📊 <b>Предложенный диапазон:</b> <code>{ranges_text}</code>"
            )
    else:
        context_lines.append(
            "📌 <i>Автоматических пометок «проповедь» не найдено.</i>"
        )

    context_block = "\n".join(context_lines)
    if context_block:
        context_block = "\n\n" + context_block

    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.row(
        InlineKeyboardButton(
            text="❌ Отменить задачу",
            callback_data=f"yd_task_cancel:{task_id}",
        ),
    )

    sent_msg = None
    try:
        await callback.message.edit_text(
            f"✏️ <b>Введите диапазон проповеди</b>\n\n"
            f"📄 Файл: <code>{file_name_esc}</code>\n"
            f"📊 Всего слайдов: <b>{total_slides}</b>"
            f"{context_block}\n\n"
            f"<b>Формат:</b> <code>5-30</code> или <code>5,7,10-15</code>\n"
            f"Отправьте текстом в чат (ответом на это сообщение).\n"
            f"<i>Отправьте <code>отмена</code> или <code>0</code>, "
            f"чтобы пропустить файл.</i>",
            parse_mode="HTML",
            reply_markup=cancel_kb.as_markup(),
        )
        sent_msg = callback.message
    except Exception as e:
        logging.error(
            f"yd_sermon_edit: edit_text упал для {task_id}: {e}",
            exc_info=True,
        )
        await asyncio.shield(_yd_cleanup_task(
            task_id,
            pending.get("session_key"),
            pending.get("task_dir"),
            pending.get("owner_user_id"),
            pending.get("chat_id"),
            pending.get("nonce"),
            bot=bot,
            status_msg=None,
            error=None,
        ))
        return

    pending["awaiting_range_for_idx"] = idx
    pending["prompt_nonce"] = manual_nonce
    pending["prompt_idx"] = idx
    if sent_msg is not None and hasattr(sent_msg, "message_id"):
        pending["prompt_message_id"] = sent_msg.message_id

    if pending.get("prompt_nonce") != manual_nonce:
        logging.info(f"yd_sermon_edit: nonce изменён конкурентно")
        return

    old_timeout = pending.get("prompt_timeout_task")
    if old_timeout is not None and not old_timeout.done():
        old_timeout.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(old_timeout), timeout=5.0
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:
            logging.debug(f"yd_sermon_edit: await old_timeout: {e}")
    pending["prompt_timeout_task"] = None

    pending["prompt_timeout_task"] = asyncio.create_task(
        _yd_prompt_timeout_watchdog(
            task_id, yandex_state.config.prompt_timeout_sec, manual_nonce
        )
    )
    pending["prompt_watchdog_nonce"] = manual_nonce


# ==========================================
# ПУБЛИЧНЫЕ ОБЁРТКИ
# ==========================================

render_sermon_prompt = _yd_render_sermon_prompt
render_category_prompt = _yd_render_category_prompt
convert_and_upload = _yd_convert_and_upload
cleanup_task = _yd_cleanup_task
is_sermon_slide = _is_sermon_slide
claim_prompt = _yd_claim_prompt
prompt_timeout_watchdog = _yd_prompt_timeout_watchdog
drain_deferred_cleanups_async = drain_deferred_cleanups
safe_answer = _safe_answer