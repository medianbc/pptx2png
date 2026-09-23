# ==========================================
# yandex_flow.py — ОРКЕСТРАЦИЯ ЯНДЕКС.ДИСКА (v3.0)
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
from typing import Optional

from aiogram import Router, F, types, Bot
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
from converter_engine import convert_all_pngs, create_zip_stream


router = Router()


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
    """Безопасный edit_text с логированием ошибок."""
    if msg is None:
        return False
    try:
        await msg.edit_text(text, **kwargs)
        return True
    except Exception as e:
        logging.warning(f"Не удалось обновить статус-сообщение: {e}", exc_info=True)
        return False


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


def _is_sermon_slide(item: dict, slide_idx: int) -> bool:
    """Проверяет, относится ли слайд к проповеди (по ranges или start..end)."""
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
    """
    Считает количество УНИКАЛЬНЫХ слайдов, попадающих в объединение диапазонов.

    ranges: список кортежей (start, end) или None
    total_slides: общее количество слайдов (для клиппинга)

    Пример:
        ranges=[(1,1),(10,10)], total=20 → 2
        ranges=[(1,5),(3,8)],   total=20 → 8  (не 11 — пересечение не двоится)
    """
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
# ПАЙПЛАЙН ПОДГОТОВКИ (без конвертации)
# ==========================================

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
):
    """
    Первая фаза: скачивание + извлечение заметок + определение проповеди.
    БЕЗ конвертации в PNG — она будет позже, после подтверждения режима.

    Сохраняет результат в sessions[task_id]["pending"].
    """
    status_msg = callback.message
    owner_user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    task_id = f"yd_task_{owner_user_id}_{secrets.token_hex(4)}"
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    status_message_id = status_msg.message_id if status_msg is not None else None

    logging.info(
        f"[YD-PREP] Старт: task_id={task_id}, файлов={len(files_to_process)}"
    )

    async with yd_session_lock:
        picker = sessions.get(session_key)
        if picker is None:
            _safe_delete_task_dir(task_dir)
            await _safe_edit(status_msg, "❌ Сессия была отменена.")
            return
        picker.setdefault("task_ids", []).append(task_id)
        sessions[task_id] = {
            "user_id": owner_user_id,
            "chat_id": chat_id,
            "pending": None,
            "nonce": nonce,
            "created_at": time.time(),
            "cancelled": False,
        }
        yd_active_tasks.add(task_id)

    try:
        target_base = paths["target"]
        quality = user_mgr.get_user_config(owner_user_id)["quality"]

        prepared = []

        for f_idx, pptx_item in enumerate(files_to_process, start=1):
            _touch_task(task_dir)

            task_sess = sessions.get(task_id)
            if task_sess is None or task_sess.get("cancelled"):
                logging.info(f"[YD-PREP] Задача {task_id} отменена — прерываем подготовку")
                await _yd_cleanup_task(
                    task_id, session_key, task_dir,
                    owner_user_id, chat_id, nonce,
                )
                return

            file_name = pptx_item["name"]
            file_name_esc = html_module.escape(file_name)
            file_size = pptx_item.get("size", 0)

            logging.info(
                f"[YD-PREP] Файл #{f_idx}/{len(files_to_process)}: "
                f"{file_name!r} ({_format_size(file_size)})"
            )

            # Скачивание с кнопкой отмены
            await _safe_edit(
                status_msg,
                f"📥 Скачиваю <code>{file_name_esc}</code>...",
                parse_mode="HTML",
                reply_markup=_get_cancel_keyboard(task_id).as_markup(),
            )

            local_pptx = task_dir / file_name
            ok = await yandex_state.config.client.download_file(
                pptx_item["path"], local_pptx
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

            # Проверяем отмену после скачивания
            task_sess = sessions.get(task_id)
            if task_sess is None or task_sess.get("cancelled"):
                logging.info(
                    f"[YD-PREP] Задача {task_id} отменена после скачивания "
                    f"{file_name} — прерываем"
                )
                await _yd_cleanup_task(
                    task_id, session_key, task_dir,
                    owner_user_id, chat_id, nonce,
                )
                return

            # ✅ Извлекаем заметки ИЗ PPTX (без конвертации — быстро)
            await _safe_edit(
                status_msg,
                f"🔍 Читаю заметки докладчика: <code>{file_name_esc}</code>...",
                parse_mode="HTML",
                reply_markup=_get_cancel_keyboard(task_id).as_markup(),
            )

            notes_ok, notes, incomplete = await asyncio.to_thread(
                extract_speaker_notes, str(local_pptx)
            )

            # Определяем проповедь по ключевым словам из конфига
            if not notes_ok:
                start, end, matches = None, None, []
                incomplete_warning = (
                    "⚠️ Не удалось прочитать заметки докладчика."
                )
            elif incomplete:
                start, end, matches = None, None, []
                incomplete_warning = (
                    "⚠️ Заметки прочитаны частично, "
                    "проповедь не определена автоматически."
                )
            else:
                # ✅ Передаём ВЕСЬ список ключевых слов
                keywords = yandex_state.config.sermon_keywords or [
                    yandex_state.config.sermon_keyword
                ]

                start, end, matches = find_sermon_range(notes, keywords)
                if matches and len(matches) == 1:
                    start, end = None, None
                incomplete_warning = None

            logging.info(
                f"[YD-PREP] {file_name}: заметок={len(notes)}, "
                f"matches={len(matches)}, start={start}, end={end}, "
                f"notes_ok={notes_ok}, incomplete={incomplete}"
            )

            item_ranges = [(start, end)] if start is not None else None

            # ✅ НЕ конвертируем пока. PNG создадим после подтверждения режима.
            prepared.append({
                "file_name": file_name,
                "file_slug": safe_folder_name(file_name),
                "file_path": local_pptx,       # ← PPTX для конвертации позже
                "start": start,
                "end": end,
                "ranges": item_ranges,
                "matches": matches,
                "notes_ok": notes_ok,
                "incomplete": incomplete,
                "incomplete_warning": incomplete_warning,
                "confirmed": False,
                "convert_mode": None,          # ← заполним в yd_sermon_mode
            })

        # Публикуем pending
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
                    }

        if cleanup_needed:
            logging.info(f"[YD-PREP] Задача {task_id} отменена до сохранения pending")
            await _yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
            )
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
            # Нет файлов для подтверждения — сразу в конвертацию с режимом both
            for item in prepared:
                if item.get("convert_mode") is None:
                    item["convert_mode"] = "both"
            await _yd_convert_and_upload(
                callback=callback,
                bot=bot,
                task_id=task_id,
                status_msg=status_msg,
            )
            return

        await _yd_ask_sermon_confirmation(
            callback=callback,
            task_id=task_id,
            status_msg=status_msg,
            needs_confirm=needs_confirm,
        )

    except Exception as e:
        logging.error(f"[YD-PREP] Ошибка: {e}", exc_info=True)
        await _yd_cleanup_task(
            task_id, session_key, task_dir,
            owner_user_id, chat_id, nonce,
            bot=bot, status_msg=status_msg, error=e,
        )


async def _yd_ask_sermon_confirmation(
    callback: types.CallbackQuery,
    task_id: str,
    status_msg,
    needs_confirm: list,
):
    if not needs_confirm:
        return
    await _yd_render_sermon_prompt(task_id, needs_confirm[0], status_msg)


# ==========================================
# ПРОМПТЫ
# ==========================================

async def _yd_claim_prompt(callback: types.CallbackQuery) -> Optional[dict]:
    """Атомарно проверяет и 'потребляет' промпт."""
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return None
    task_id = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return None
    nonce = parts[3]

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await callback.answer("❌ Сессия неактивна.", show_alert=True)
        return None
    if session.get("cancelled"):
        await callback.answer("❌ Задача отменена.", show_alert=True)
        return None

    pending = session["pending"]
    if not isinstance(pending, dict):
        await callback.answer("❌ Сессия неактивна.", show_alert=True)
        return None

    if pending.get("prompt_nonce") != nonce or pending.get("prompt_idx") != idx:
        await callback.answer("⏳ Промпт уже обработан.", show_alert=True)
        return None

    if callback.from_user.id != pending["owner_user_id"]:
        await callback.answer("❌ Только автор.", show_alert=True)
        return None

    timeout_task = pending.get("prompt_timeout_task")
    if timeout_task is not None and not timeout_task.done():
        timeout_task.cancel()
    pending["prompt_timeout_task"] = None
    pending["prompt_watchdog_nonce"] = None

    pending["prompt_nonce"] = None
    pending["prompt_idx"] = None
    pending["prompt_message_id"] = None

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    return pending


async def _yd_render_sermon_prompt(
    task_id: str,
    item: dict,
    status_msg,
    reply_fn=None,
) -> None:
    """
    Единая точка отрисовки промпта подтверждения проповеди.

    Три варианта:
      1. has_valid_range = True → 3 кнопки режимов + Изменить + Отмена
      2. matches > 0, но одна пометка → 2 кнопки + Изменить + Отмена
      3. matches = 0 / notes не прочитаны → «Конвертировать всё» + Изменить + Отмена

    ✅ Исправление бага №4: подсчёт и отображение диапазона ведётся
    по item["ranges"] (если он есть), а не по схлопнутым start/end.
    Для ввода вида "1,10" это даёт 2 слайда и текст "1, 10",
    а не 10 слайдов и текст "1–10".
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
            f"_yd_render_sermon_prompt: item не найден в prepared "
            f"для task_id={task_id}"
        )
        return

    pending["prompt_idx"] = idx

    if pending.get("prompt_nonce") is None:
        pending["prompt_nonce"] = secrets.token_hex(8)
    prompt_nonce = pending["prompt_nonce"]

    matches = item.get("matches", []) or []
    start = item.get("start")
    end = item.get("end")
    ranges = item.get("ranges")
    file_name = item.get("file_name", "")
    file_esc = html_module.escape(file_name)

    # ✅ Общее количество слайдов — считаем из PPTX (без конвертации)
    file_path = item.get("file_path")
    total_slides = 0
    if file_path and Path(file_path).exists():
        try:
            from pptx import Presentation
            prs = Presentation(str(file_path))
            total_slides = len(prs.slides._sldIdLst)
        except Exception as e:
            logging.warning(f"Не удалось определить число слайдов: {e}")

    kb = InlineKeyboardBuilder()

    has_valid_range = (
        bool(matches)
        and start is not None
        and end is not None
        and start <= end
    )

    logging.debug(
        f"[YD-PROMPT] task_id={task_id}, idx={idx}, file={file_name!r}, "
        f"matches={len(matches)}, start={start}, end={end}, ranges={ranges}, "
        f"has_valid_range={has_valid_range}, total_slides={total_slides}"
    )

    if has_valid_range:
        # --- Диапазон задан (автоматически или вручную) ---

        # ✅ Баг №4: считаем sermon_count по ranges, если они есть.
        # Это корректно обрабатывает несвязные диапазоны (1,10 → 2 слайда).
        if ranges:
            sermon_count = _count_slides_in_ranges(ranges, total_slides)
            ranges_text = _format_ranges_text(ranges, start, end)
        else:
            sermon_count = _count_slides_for_range(start, end, total_slides)
            ranges_text = _format_ranges_text(None, start, end)

        other_count = max(0, total_slides - sermon_count)

        manual_range = item.get("manual_range", False)

        if manual_range:
            # Ручной ввод — не говорим «найдена пометка»
            text = (
                f"🎯 <b>Диапазон проповеди установлен</b>\n\n"
                f"📄 Файл: <code>{file_esc}</code>\n"
                f"📊 Диапазон: <b>{ranges_text}</b>\n"
                f"🎯 Слайдов проповеди: <b>{sermon_count}</b>\n\n"
                f"❓ <b>Какие слайды конвертировать?</b>"
            )
        else:
            # Автоматический поиск — показываем пометки
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
        # Проповедь не найдена — предлагаем только «конвертировать всё»
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
            reason = (
                "В заметках докладчика нет слова «проповедь»."
            )

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

    # Общая кнопка отмены для всех вариантов
    kb.row(
        InlineKeyboardButton(
            text="❌ Отменить задачу",
            callback_data=f"yd_task_cancel:{task_id}",
        ),
    )

    sent_msg = None
    if reply_fn is not None:
        sent_msg = await reply_fn(text, parse_mode="HTML", reply_markup=kb.as_markup())
    elif status_msg is not None:
        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        sent_msg = status_msg
    else:
        logging.warning(
            f"_yd_render_sermon_prompt: нет ни reply_fn, ни status_msg для {task_id}"
        )
        return

    if pending.get("prompt_nonce") != prompt_nonce:
        logging.info(
            f"_yd_render_sermon_prompt: nonce изменён конкурентно для {task_id}"
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

async def _yd_prompt_timeout_watchdog(task_id: str, timeout_sec: int, expected_nonce: str):
    """Если пользователь не ответил на промпт — уведомляем и очищаем."""
    try:
        await asyncio.sleep(timeout_sec)
        session = sessions.get(task_id)
        if not session or "pending" not in session:
            return
        pending = session["pending"]
        if not isinstance(pending, dict):
            return
        if pending.get("prompt_nonce") != expected_nonce:
            return

        chat_id = pending.get("chat_id")
        bot: Optional[Bot] = pending.get("bot")
        owner_user_id = pending.get("owner_user_id")
        session_key = pending.get("session_key")
        task_dir = pending.get("task_dir")
        nonce = pending.get("nonce")
        prompt_message_id = pending.get("prompt_message_id")

        logging.info(
            f"[YD-PROMPT] ⏰ Промпт {task_id} не подтверждён за {timeout_sec}s — очистка"
        )

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
            except Exception:
                pass

        await _yd_cleanup_task(
            task_id, session_key, task_dir,
            owner_user_id, chat_id, nonce,
        )
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
    logging.info(
        f"[YD-CLEANUP] task_id={task_id}, session_key={session_key!r}, "
        f"reason={'error' if error else 'normal'}"
    )

    # Снимаем клавиатуру со всех сообщений задачи
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
                            logging.debug(
                                f"[YD-CLEANUP] Снята клавиатура с сообщения {mid}"
                            )
                        except Exception as e:
                            logging.debug(
                                f"[YD-CLEANUP] Не удалось снять клавиатуру "
                                f"с {mid}: {e}"
                            )
    except Exception as e:
        logging.debug(f"[YD-CLEANUP] Ошибка снятия клавиатуры: {e}")

    # Папка задачи
    try:
        if task_dir and task_dir.exists():
            shutil.rmtree(task_dir)
            logging.info(f"🧹 Удалена папка задачи: {task_dir}")
    except Exception as e:
        logging.error(f"Ошибка удаления task_dir {task_dir}: {e}")

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
    except Exception as e:
        logging.error(f"Ошибка отмены watchdog для {task_id}: {e}", exc_info=True)

    # Сессии
    try:
        async with yd_session_lock:
            picker = sessions.get(session_key)
            if picker is not None:
                picker["processing"] = False
                task_ids = picker.get("task_ids")
                if isinstance(task_ids, list) and task_id in task_ids:
                    task_ids.remove(task_id)

            current = sessions.get(session_key)
            if current is not None and current.get("nonce") == nonce:
                sessions.pop(session_key, None)

            sessions.pop(task_id, None)
            yd_active_tasks.discard(task_id)
    except Exception as e:
        logging.error(f"Ошибка очистки сессий для {task_id}: {e}", exc_info=True)

    # yd_release
    try:
        await yd_release(owner_user_id, chat_id, nonce)
    except Exception as e:
        logging.error(f"Ошибка yd_release для {task_id}: {e}", exc_info=True)

    # Сообщение об ошибке
    if error is not None and bot is not None and status_msg is not None:
        try:
            await status_msg.edit_text(
                f"❌ <b>Ошибка обработки</b>\n\n"
                f"<code>{html_module.escape(str(error)[:200])}</code>\n\n"
                f"Временные файлы удалены. Попробуйте снова.",
                parse_mode="HTML",
            )
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения для {task_id}: {e}")


# ==========================================
# КОНВЕРТАЦИЯ + UPLOAD (после выбора режима)
# ==========================================

async def _yd_convert_and_upload(
    callback: types.CallbackQuery,
    bot: Bot,
    task_id: str,
    status_msg,
):
    """
    Вторая фаза: конвертация PNG + упаковка в ZIP + upload.
    Вызывается после выбора режима в yd_sermon_mode.
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
            await _yd_cleanup_task(
                task_id,
                pending.get("session_key"),
                pending.get("task_dir"),
                pending.get("owner_user_id"),
                pending.get("chat_id"),
                pending.get("nonce"),
            )
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

    # Обновляем status_message_id
    if status_msg is not None and hasattr(status_msg, "message_id"):
        pending["status_message_id"] = status_msg.message_id

    zip_tmp_dir: Optional[Path] = None
    cleanup_done = False

    logging.info(
        f"[YD-UP] Старт: task_id={task_id}, файлов={len(prepared)}, "
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

        for f_idx, item in enumerate(prepared, start=1):
            _touch_task(task_dir)

            if _is_cancelled():
                logging.info(f"[YD-UP] Отмена перед файлом #{f_idx}")
                return

            file_name = item["file_name"]
            file_name_esc = html_module.escape(file_name)
            file_slug = item["file_slug"]

            if item.get("failed_at_stage"):
                stage = item["failed_at_stage"]
                stage_text = {
                    "download": "ошибка скачивания",
                    "convert": "ошибка конвертации",
                    "no_pngs": "нет PNG",
                }.get(stage, stage)
                report_lines.append(f"❌ {file_name_esc} — {stage_text}")
                total_failed += 1
                continue

            # Путь к PPTX (скачан в _yd_prepare_files)
            file_path = item.get("file_path")
            if not file_path or not Path(file_path).exists():
                logging.error(f"[YD-UP] {file_name}: PPTX не найден ({file_path})")
                report_lines.append(f"❌ {file_name_esc} — файл PPTX не найден")
                total_failed += 1
                continue

            start = item.get("start")
            end = item.get("end")
            ranges = item.get("ranges")
            convert_mode = item.get("convert_mode", "both")
            incomplete_warning = item.get("incomplete_warning")

            logging.info(
                f"[YD-UP] Файл #{f_idx}: {file_name!r}, "
                f"convert_mode={convert_mode}, ranges={ranges}"
            )

            # === Конвертация PNG ===
            await _safe_edit(
                status_msg,
                f"⚙️ Конвертирую <code>{file_name_esc}</code> в PNG...",
                parse_mode="HTML",
                reply_markup=_get_cancel_keyboard(task_id).as_markup(),
            )

            temp_png_dir = task_dir / f"png_{f_idx}"
            temp_png_dir.mkdir(exist_ok=True)

            try:
                pngs, used_pptx = await convert_all_pngs(
                    Path(file_path), temp_png_dir, quality
                )
            except Exception as e:
                logging.error(
                    f"[YD-UP] {file_name}: ошибка конвертации: {e}",
                    exc_info=True,
                )
                report_lines.append(f"❌ {file_name_esc} — ошибка конвертации")
                total_failed += 1
                continue

            if not pngs:
                logging.warning(f"[YD-UP] {file_name}: не создано ни одного PNG")
                report_lines.append(f"❌ {file_name_esc} — нет PNG")
                total_failed += 1
                continue

            pngs_sorted = sorted(pngs, key=lambda p: p.name)

            if _is_cancelled():
                logging.info(f"[YD-UP] Отмена после конвертации {file_name}")
                return

            # === Разделение PNG на sermon / others ===
            sermon_pngs = []
            other_pngs = []
            for slide_idx, png_path in enumerate(pngs_sorted, start=1):
                if _is_sermon_slide(item, slide_idx):
                    sermon_pngs.append(png_path)
                else:
                    other_pngs.append(png_path)

            ranges_text = _format_ranges_text(ranges, start, end)

            # === Создаём целевые папки на Диске ===
            pptx2png_dir = (
                f"{target_base}/{yandex_state.config.pptx2png_folder}/{file_slug}"
            )
            sermon_dir = f"{target_base}/{yandex_state.config.sermon_folder}"

            need_pptx2png_folder = convert_mode in ("other", "both")
            need_sermon_folder = convert_mode in ("sermon", "both")

            if need_pptx2png_folder:
                ok1 = await yandex_state.config.client.ensure_folder(pptx2png_dir)
                if not ok1:
                    logging.error(
                        f"[YD-UP] {file_name}: не удалось создать {pptx2png_dir!r}"
                    )
                    report_lines.append(
                        f"❌ {file_name_esc} — не удалось создать папки"
                    )
                    total_failed += 1
                    continue

            if need_sermon_folder and sermon_pngs:
                ok2 = await yandex_state.config.client.ensure_folder(sermon_dir)
                if not ok2:
                    logging.error(
                        f"[YD-UP] {file_name}: не удалось создать {sermon_dir!r}"
                    )
                    report_lines.append(
                        f"❌ {file_name_esc} — не удалось создать папку проповеди"
                    )
                    total_failed += 1
                    continue

            # === Архив с проповедью ===
            sermon_info = None
            if need_sermon_folder and sermon_pngs:
                if _is_cancelled():
                    logging.info(f"[YD-UP] Отмена перед ZIP проповеди")
                    return

                sermon_zip_name = f"{file_slug}_проповедь.zip"
                sermon_zip_path = zip_tmp_dir / sermon_zip_name

                try:
                    await asyncio.to_thread(
                        create_zip_stream, sermon_pngs, sermon_zip_path
                    )
                except Exception as e:
                    logging.error(
                        f"[YD-UP] {file_name}: ошибка ZIP проповеди: {e}",
                        exc_info=True,
                    )
                    report_lines.append(
                        f"❌ {file_name_esc} — ошибка упаковки проповеди"
                    )
                    total_failed += 1
                    continue

                if _is_cancelled():
                    _safe_unlink(sermon_zip_path)
                    return

                sermon_zip_size = sermon_zip_path.stat().st_size
                remote_path = f"{sermon_dir}/{sermon_zip_name}"

                await _safe_edit(
                    status_msg,
                    f"📤 Загружаю архив проповеди "
                    f"(<code>{file_name_esc}</code>)...",
                    parse_mode="HTML",
                    reply_markup=_get_cancel_keyboard(task_id).as_markup(),
                )

                if _is_cancelled():
                    _safe_unlink(sermon_zip_path)
                    return

                ok = await yandex_state.config.client.upload_file(
                    sermon_zip_path, remote_path
                )

                if ok:
                    total_uploaded_zip += 1
                    total_slides_packed += len(sermon_pngs)
                    links_by_folder[sermon_dir] = "🎯 Проповедь"
                    sermon_info = (
                        f"🎯 Проповедь ({ranges_text}): {len(sermon_pngs)} слайдов → "
                        f"<code>{html_module.escape(yandex_state.config.sermon_folder)}/"
                        f"{html_module.escape(sermon_zip_name)}</code> "
                        f"({_format_size(sermon_zip_size)})"
                    )
                    logging.info(
                        f"[YD-UP] {file_name}: sermon ZIP загружен "
                        f"({len(sermon_pngs)} слайдов)"
                    )
                else:
                    total_failed += 1
                    sermon_info = (
                        f"❌ Проповедь ({ranges_text}): "
                        f"не удалось загрузить ZIP"
                    )
                    logging.error(f"[YD-UP] {file_name}: sermon ZIP failed")

                _safe_unlink(sermon_zip_path)
                for png in sermon_pngs:
                    _safe_unlink(png)

            # === Архив с остальными слайдами ===
            other_info = None
            if need_pptx2png_folder and other_pngs:
                if _is_cancelled():
                    logging.info(f"[YD-UP] Отмена перед ZIP слайдов")
                    return

                other_zip_name = f"{file_slug}_слайды.zip"
                other_zip_path = zip_tmp_dir / other_zip_name

                try:
                    await asyncio.to_thread(
                        create_zip_stream, other_pngs, other_zip_path
                    )
                except Exception as e:
                    logging.error(
                        f"[YD-UP] {file_name}: ошибка ZIP слайдов: {e}",
                        exc_info=True,
                    )
                    report_lines.append(
                        f"❌ {file_name_esc} — ошибка упаковки слайдов"
                    )
                    total_failed += 1
                    if sermon_info:
                        report_lines.append(f"{f_idx}. 📄 <b>{file_name_esc}</b>")
                        report_lines.append(f"   • {sermon_info}")
                    continue

                if _is_cancelled():
                    _safe_unlink(other_zip_path)
                    return

                other_zip_size = other_zip_path.stat().st_size
                remote_path = f"{pptx2png_dir}/{other_zip_name}"

                await _safe_edit(
                    status_msg,
                    f"📤 Загружаю архив слайдов "
                    f"(<code>{file_name_esc}</code>)...",
                    parse_mode="HTML",
                    reply_markup=_get_cancel_keyboard(task_id).as_markup(),
                )

                if _is_cancelled():
                    _safe_unlink(other_zip_path)
                    return

                ok = await yandex_state.config.client.upload_file(
                    other_zip_path, remote_path
                )

                other_folder_short = (
                    f"{yandex_state.config.pptx2png_folder}/{file_slug}"
                )

                if ok:
                    total_uploaded_zip += 1
                    total_slides_packed += len(other_pngs)
                    links_by_folder[pptx2png_dir] = "📄 Остальные слайды"
                    other_info = (
                        f"📄 Остальные: {len(other_pngs)} слайдов → "
                        f"<code>{html_module.escape(other_folder_short)}/"
                        f"{html_module.escape(other_zip_name)}</code> "
                        f"({_format_size(other_zip_size)})"
                    )
                    logging.info(
                        f"[YD-UP] {file_name}: slides ZIP загружен "
                        f"({len(other_pngs)} слайдов)"
                    )
                else:
                    total_failed += 1
                    other_info = (
                        "❌ Остальные слайды: не удалось загрузить ZIP"
                    )
                    logging.error(f"[YD-UP] {file_name}: slides ZIP failed")

                _safe_unlink(other_zip_path)
                for png in other_pngs:
                    _safe_unlink(png)

            # Если режим не включал ни одной папки — фиксируем
            if not sermon_info and not other_info:
                if convert_mode == "sermon" and not sermon_pngs:
                    other_info = "ℹ️ Проповедь не найдена в этом файле"
                elif convert_mode == "other" and not other_pngs:
                    other_info = "ℹ️ Все слайды относятся к проповеди"
                else:
                    other_info = "ℹ️ Нет слайдов для конвертации"

            # Формируем запись отчёта
            entry_lines = [f"{f_idx}. 📄 <b>{file_name_esc}</b>"]
            if sermon_info:
                entry_lines.append(f"   • {sermon_info}")
            if other_info:
                entry_lines.append(f"   • {other_info}")
            if incomplete_warning:
                entry_lines.append(f"   • {incomplete_warning}")
            report_lines.append("\n".join(entry_lines))

        if _is_cancelled():
            logging.info(f"[YD-UP] Отмена перед отправкой отчёта")
            return

        # Итог
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

        # Снимаем клавиатуру — работа завершена
        try:
            await status_msg.edit_reply_markup(reply_markup=None)
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
            f"[YD-UP] Итог: zip={total_uploaded_zip}, "
            f"slides={total_slides_packed}, failed={total_failed}"
        )

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
            logging.error(f"Ошибка edit_text первой части: {e}", exc_info=True)
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
                logging.error(f"Не удалось отправить первую часть: {e2}", exc_info=True)
                failed_chunks.append(1)

    if not first_delivered:
        try:
            fallback = f"⚠️ Не удалось показать полный отчёт.\n📊 Загружено: {total_uploaded}"
            if total_failed:
                fallback += f"\n❌ Ошибок: {total_failed}"
            await bot.send_message(chat_id=chat_id, text=fallback)
        except Exception as e:
            logging.error(f"Не удалось отправить fallback-отчёт: {e}", exc_info=True)

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
        await message.reply("❌ Яндекс.Диск не настроен. Обратитесь к администратору.")
        return

    if not yandex_state.config.base_path:
        await message.reply("❌ Не задан base_path Яндекс.Диска в settings.ini.")
        return

    logging.info(
        f"[YD] /sunday от user={message.from_user.id}, "
        f"base_path={yandex_state.config.base_path!r}"
    )

    nonce = await yd_try_acquire(message.from_user.id, message.chat.id)
    if nonce is None:
        await message.reply(
            "⏳ У вас уже активна сессия подготовки трансляции.\n"
            "Дождитесь завершения или нажмите /cancel_yd."
        )
        return

    status_msg = None
    session_created = False

    try:
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
            f"📅 Ближайшее воскресенье: <b>{html_module.escape(sunday_str)}</b>\n"
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
                f"📍 Папка: <code>{html_module.escape(paths['source'])}</code>\n\n"
                f"❌ <b>pptx-файлы не найдены.</b>\n\n"
                f"Положите pptx с датой <code>{sunday:%d.%m.%y}</code> "
                f"в папку <code>{src_esc}</code> и попробуйте снова.",
                parse_mode="HTML",
            )
            return

        MAX_LEN = 3500
        header_lines = [
            f"📅 Ближайшее воскресенье: <b>{html_module.escape(sunday_str)}</b>",
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
            body_lines.append(f"…и ещё <b>{omitted}</b> файл(ов) не показано.")

        body_lines.append("")
        body_lines.append("🎬 Выберите файл для обработки:")

        if not await yd_is_active(message.from_user.id, message.chat.id, nonce):
            logging.info(
                f"[YD] Сессия {message.from_user.id}:{message.chat.id} "
                f"была отменена во время выполнения"
            )
            try:
                await status_msg.edit_text("❌ Операция отменена пользователем.")
            except Exception:
                pass
            return

        session_key = f"yd_{message.from_user.id}_{message.chat.id}"

        async with yd_session_lock:
            old_picker = sessions.get(session_key)
            if old_picker is not None:
                for old_tid in old_picker.get("task_ids", []):
                    old_sess = sessions.get(old_tid)
                    if old_sess is not None:
                        old_sess["cancelled"] = True

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
            }

        kb = InlineKeyboardBuilder()
        for idx, f in enumerate(pptx_files):
            prefix = "🎯" if "служение" in f["name"].lower() else "📄"
            kb.row(InlineKeyboardButton(
                text=f"{prefix} {f['name']}",
                callback_data=f"yd_pick:{message.from_user.id}:{nonce}:{idx}",
            ))
        if len(pptx_files) > 1:
            kb.row(InlineKeyboardButton(
                text="📁 Все подряд",
                callback_data=f"yd_pick:{message.from_user.id}:{nonce}:all",
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
            f"освобождена после показа списка (nonce={nonce[:8]}...)"
        )

    except Exception as e:
        logging.error(f"[YD] Ошибка cmd_sunday: {e}", exc_info=True)
        try:
            if status_msg:
                await status_msg.edit_text(
                    f"❌ Ошибка: <code>{html_module.escape(str(e)[:200])}</code>",
                    parse_mode="HTML",
                )
            else:
                await message.reply(f"❌ Ошибка: {str(e)[:200]}")
        except Exception:
            pass
    finally:
        if not session_created:
            released = await yd_release(message.from_user.id, message.chat.id, nonce)
            if released:
                logging.info(
                    f"[YD] 🔓 Сессия {message.from_user.id}:{message.chat.id} "
                    f"освобождена (неудачный запуск, nonce={nonce[:8]}...)"
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
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return

    try:
        owner_user_id = int(parts[1])
    except ValueError:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return

    callback_nonce = parts[2]
    file_selector = parts[3]

    if callback.from_user.id != owner_user_id:
        await callback.answer("❌ Только автор запроса может выбрать файл.", show_alert=True)
        return

    session_key = f"yd_{owner_user_id}_{callback.message.chat.id}"

    async with yd_session_lock:
        session = sessions.get(session_key)
        if not session or session.get("nonce") != callback_nonce:
            await callback.answer("❌ Сессия неактивна.", show_alert=True)
            return

        if session.get("processing"):
            await callback.answer("⏳ Обработка уже запущена.", show_alert=True)
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
                    await callback.answer("❌ Файл не найден.", show_alert=True)
                    return
                files_to_process = [files[idx]]
            except ValueError:
                session.pop("processing", None)
                await callback.answer("❌ Некорректный выбор.", show_alert=True)
                return

    if file_selector == "all":
        await callback.answer("⏳ Обрабатываю все файлы...")
    else:
        await callback.answer("⏳ Начинаю обработку...")

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


@router.callback_query(F.data.startswith("yd_cancel:"))
async def yd_cancel_callback(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return

    try:
        owner_user_id = int(parts[1])
    except ValueError:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return

    callback_nonce = parts[2]

    if callback.from_user.id != owner_user_id:
        await callback.answer(
            "❌ Только автор запроса может отменить операцию.", show_alert=True
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
        await yd_release(owner_user_id, callback.message.chat.id, callback_nonce)
        try:
            await callback.message.edit_text("❌ Сессия уже неактивна.")
        except Exception:
            pass
        await callback.answer("❌ Сессия уже неактивна.", show_alert=True)
        return

    for tid in task_ids_to_cancel:
        async with yd_session_lock:
            task_sess = sessions.get(tid)
            if task_sess is not None:
                task_sess["cancelled"] = True

    await yd_release(owner_user_id, callback.message.chat.id, callback_nonce)

    try:
        await callback.message.edit_text("❌ Операция отменена.")
    except Exception:
        pass
    await callback.answer()


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

    for tid in task_ids_to_cancel:
        async with yd_session_lock:
            task_sess = sessions.get(tid)
            if task_sess is not None:
                task_sess["cancelled"] = True

    released = await yd_release(message.from_user.id, message.chat.id)

    if session is None and not task_ids_to_cancel and released:
        await message.reply("ℹ️ У вас нет активной сессии Яндекс.Диска.")
        return

    await message.reply("✅ Сессия Яндекс.Диска сброшена.")


# ==========================================
# НОВЫЙ ХЕНДЛЕР: выбор режима конвертации
# ==========================================

@router.callback_query(F.data.startswith("yd_sermon_mode:"))
async def yd_sermon_mode(callback: types.CallbackQuery, bot: Bot):
    """
    Пользователь выбрал режим конвертации:
      - sermon → только проповедь
      - other  → только остальные
      - both   → оба ZIP раздельно
    """
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return

    task_id = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return
    nonce = parts[3]
    mode = parts[4]

    if mode not in ("sermon", "other", "both"):
        await callback.answer("❌ Неизвестный режим.", show_alert=True)
        return

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await callback.answer("❌ Сессия неактивна.", show_alert=True)
        return

    claimed = await _yd_claim_prompt(callback)
    if claimed is None:
        return
    pending = claimed

    item = pending["prepared"][idx]

    # Для режимов sermon / both — диапазон должен быть задан
    if mode in ("sermon", "both") and (
        item.get("start") is None or item.get("end") is None
    ):
        await callback.answer(
            "❌ Диапазон не задан. Укажите его вручную.",
            show_alert=True,
        )
        await _yd_render_sermon_prompt(task_id, item, callback.message)
        return

    item["convert_mode"] = mode
    item["confirmed"] = True

    try:
        await callback.answer("⏳ Принято, начинаю конвертацию...")
    except Exception:
        pass

    # Проверяем, остались ли неподтверждённые файлы
    remaining = [
        p for p in pending["prepared"]
        if "file_path" in p and not p.get("confirmed")
    ]

    if remaining:
        # Показываем промпт для следующего файла
        await _yd_render_sermon_prompt(task_id, remaining[0], callback.message)
        return

    # Все подтверждены — конвертируем и загружаем
    await _yd_convert_and_upload(
        callback=callback,
        bot=bot,
        task_id=task_id,
        status_msg=callback.message,
    )


# ==========================================
# НОВЫЙ ХЕНДЛЕР: отмена текущей задачи
# ==========================================

@router.callback_query(F.data.startswith("yd_task_cancel:"))
async def yd_task_cancel_callback(callback: types.CallbackQuery):
    """
    Отмена текущей Yandex-задачи.

    Работает на любой стадии:
      - До старта (pending=None) → ставим cancelled, задача отменится
        на ближайшей проверке.
      - На промпте → cleanup сразу.
      - На стадии upload → текущий шаг доводится до конца, потом cleanup.
    """
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return

    task_id = parts[1]

    session = sessions.get(task_id)
    if not session:
        logging.info(
            f"[YD-TASK-CANCEL] Задача {task_id!r} не найдена (уже очищена)"
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.answer(
            "ℹ️ Эта задача уже завершена или отменена.",
            show_alert=True,
        )
        return

    owner_user_id = session.get("user_id")
    if callback.from_user.id != owner_user_id:
        await callback.answer(
            "❌ Только автор задачи может её отменить.",
            show_alert=True,
        )
        return

    # ✅ Ставим cancelled=True ВСЕГДА
    session["cancelled"] = True

    pending = session.get("pending")

    # Случай 1: pending ещё не создан (подготовка не завершена)
    if not isinstance(pending, dict):
        logging.info(
            f"[YD-TASK-CANCEL] Задача {task_id!r} отменена на стадии подготовки"
        )

        try:
            await callback.answer("❌ Отмена запрошена")
        except Exception:
            pass

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

    # Случай 2: pending есть
    logging.info(
        f"[YD-TASK-CANCEL] Пользователь {callback.from_user.id} "
        f"отменил задачу {task_id}"
    )

    try:
        await callback.answer("❌ Задача отменена")
    except Exception:
        pass

    timeout_task = pending.get("prompt_timeout_task")
    if timeout_task is not None and not timeout_task.done():
        timeout_task.cancel()
    pending["prompt_timeout_task"] = None
    pending["prompt_watchdog_nonce"] = None

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

        await _yd_cleanup_task(
            task_id=task_id,
            session_key=pending.get("session_key"),
            task_dir=pending.get("task_dir"),
            owner_user_id=owner_user_id,
            chat_id=pending.get("chat_id"),
            nonce=pending.get("nonce"),
        )
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


# ==========================================
# ИЗМЕНЕНИЕ ДИАПАЗОНА ПРОПОВЕДИ (ручной ввод)
# ==========================================

@router.callback_query(F.data.startswith("yd_sermon_edit:"))
async def yd_sermon_edit(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return
    task_id = parts[1]

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await callback.answer("❌ Сессия неактивна.", show_alert=True)
        return

    claimed = await _yd_claim_prompt(callback)
    if claimed is None:
        return
    pending = claimed

    idx = int(parts[2])
    manual_nonce = secrets.token_hex(8)

    current = pending["prepared"][idx]
    file_name_esc = html_module.escape(current["file_name"])

    # ✅ Считаем total_slides через PPTX (без конвертации)
    file_path = current.get("file_path")
    total_slides = 0
    if file_path and Path(file_path).exists():
        try:
            from pptx import Presentation
            prs = Presentation(str(file_path))
            total_slides = len(prs.slides._sldIdLst)
        except Exception as e:
            logging.warning(f"Не удалось определить число слайдов: {e}")

    # Контекст о найденных слайдах
    matches = current.get("matches", []) or []
    start = current.get("start")
    end = current.get("end")
    ranges = current.get("ranges")
    notes_ok = current.get("notes_ok", True)
    incomplete = current.get("incomplete", False)

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
    elif not notes_ok:
        context_lines.append("⚠️ <i>Заметки докладчика не удалось прочитать.</i>")
    elif incomplete:
        context_lines.append("⚠️ <i>Заметки прочитаны частично.</i>")
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
        await callback.answer()
        sent_msg = await callback.message.edit_text(
            f"✏️ <b>Введите диапазон проповеди</b>\n\n"
            f"📄 Файл: <code>{file_name_esc}</code>\n"
            f"📊 Всего слайдов: <b>{total_slides}</b>"
            f"{context_block}\n\n"
            f"<b>Формат:</b> <code>5-30</code> или <code>5,7,10-15</code>\n"
            f"Отправьте текстом в чат (ответом на это сообщение).\n"
            f"<i>Отправьте <code>отмена</code> или <code>0</code>, чтобы пропустить файл.</i>",
            parse_mode="HTML",
            reply_markup=cancel_kb.as_markup(),
        )
    except Exception as e:
        logging.error(
            f"yd_sermon_edit: edit_text упал для {task_id}: {e}",
            exc_info=True,
        )
        await _yd_cleanup_task(
            task_id,
            pending.get("session_key"),
            pending.get("task_dir"),
            pending.get("owner_user_id"),
            pending.get("chat_id"),
            pending.get("nonce"),
            bot=bot,
            status_msg=None,
            error=None,
        )
        try:
            await bot.send_message(
                chat_id=callback.message.chat.id,
                text=(
                    "❌ Не удалось показать форму ввода. Задача сброшена."
                ),
            )
        except Exception:
            pass
        return

    pending["awaiting_range_for_idx"] = idx
    pending["prompt_nonce"] = manual_nonce
    pending["prompt_idx"] = idx
    if sent_msg is not None and hasattr(sent_msg, "message_id"):
        pending["prompt_message_id"] = sent_msg.message_id

    if pending.get("prompt_nonce") != manual_nonce:
        logging.info(
            f"yd_sermon_edit: nonce изменён конкурентно для {task_id}"
        )
        return

    old_timeout = pending.get("prompt_timeout_task")
    if old_timeout is not None and not old_timeout.done():
        old_timeout.cancel()

    pending["prompt_timeout_task"] = asyncio.create_task(
        _yd_prompt_timeout_watchdog(
            task_id, yandex_state.config.prompt_timeout_sec, manual_nonce
        )
    )
    pending["prompt_watchdog_nonce"] = manual_nonce


# ==========================================
# ПУБЛИЧНЫЕ ОБЁРТКИ ДЛЯ handlers.py
# ==========================================

render_sermon_prompt = _yd_render_sermon_prompt
convert_and_upload = _yd_convert_and_upload
cleanup_task = _yd_cleanup_task
is_sermon_slide = _is_sermon_slide
claim_prompt = _yd_claim_prompt
prompt_timeout_watchdog = _yd_prompt_timeout_watchdog