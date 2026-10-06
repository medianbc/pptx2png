# ==========================================
# yandex_flow_handlers.py — ХЕНДЛЕРЫ (v4.0, часть 2/3)
# ==========================================
# Все @router.callback_query и @router.message, связанные с /sunday.
# Импортирует yandex_flow_core.
# ==========================================

import asyncio
import html as html_module
import logging
import secrets
import time
from pathlib import Path

from aiogram import Router, F, types, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import yandex_state
from .yandex_state import (
    sessions,
    yd_session_lock,
    yd_active_tasks,
    yd_try_acquire,
    yd_release,
    yd_is_active,
)

from pptx2png_core.yandex_disk import (
    YandexDiskError,
    get_nearest_sunday,
    month_folder_name,
    resolve_sunday_paths,
    find_pptx_in_source,
)

from .yandex_flow_core import (
    # утилиты
    _safe_answer,
    _safe_edit,
    _cat_order,
    _cat_meta,
    _format_ranges_text,
    _normalize_item_ranges,
    _render_category_prompt_text,
    _render_category_toggle_keyboard,
    _picker_is_active,
    _picker_status,
    _picker_has_live_tasks_locked,
    _spawn_cancel_worker,
    _yd_stop_spinner,
    # промпты
    _yd_claim_prompt,
    _yd_render_category_prompt,
    _yd_render_sermon_prompt,
    # пайплайн
    _yd_prepare_files,
    _yd_convert_and_upload,
    _yd_cleanup_task,
    _yd_prompt_timeout_watchdog,   # ✅ v4.0.1: вынес из proxy
)


router = Router()


# ==========================================
# /sunday
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


# ==========================================
# yd_pick
# ==========================================

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


# ==========================================
# yd_cancel
# ==========================================

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


# ==========================================
# /cancel_yd
# ==========================================

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
# v4.0: yd_cat_toggle
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

    # ✅ v4.0.1: задача могла быть отменена, но ещё не удалена из sessions
    # (worker завершается в фоне). Не даём toggle менять состояние.
    if session.get("cancelled"):
        await _safe_answer(callback, "❌ Задача отменена.", show_alert=True)
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

    # ✅ v4.0.1: дожидаемся отмены старого watchdog'а, чтобы он не успел
    # дойти до _yd_cleanup_task между cancel() и созданием нового.
    existing_task = pending.get("prompt_timeout_task")
    if existing_task is not None and not existing_task.done():
        existing_task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(existing_task), timeout=5.0
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:
            logging.debug(f"yd_cat_toggle: await old_timeout: {e}")
    pending["prompt_timeout_task"] = None

    pending["prompt_timeout_task"] = asyncio.create_task(
        _yd_prompt_timeout_watchdog(
            task_id, yandex_state.config.prompt_timeout_sec, nonce
        )
    )
    pending["prompt_watchdog_nonce"] = nonce

    await _safe_answer(callback)


# ==========================================
# v4.0: yd_cat_convert
# ==========================================

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
        await _yd_render_category_prompt(
            task_id=task_id, item=item, status_msg=callback.message,
        )
        return

    item["confirmed"] = True

    await _safe_answer(callback, "⏳ Принято, начинаю конвертацию...")

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


# ==========================================
# Ручное редактирование диапазона категории
# ==========================================

@router.callback_query(F.data.startswith("yd_cat_edit:"))
async def yd_cat_edit(callback: types.CallbackQuery, bot: Bot):
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

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return

    pending = session["pending"]
    if not isinstance(pending, dict):
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return
    if (
        pending.get("prompt_nonce") != parts[3]
        or pending.get("prompt_idx") != idx
        or callback.from_user.id != pending.get("owner_user_id")
    ):
        await _safe_answer(callback, "⏳ Промпт уже обработан.", show_alert=True)
        return
    if idx < 0 or idx >= len(pending.get("prepared", [])):
        await _safe_answer(callback, "❌ Файл не найден.", show_alert=True)
        return

    pending = await _yd_claim_prompt(callback)
    if pending is None:
        return

    item = pending["prepared"][idx]
    file_name = html_module.escape(item.get("file_name", ""))
    total_slides = item.get("total_slides", 0)
    picker_nonce = secrets.token_hex(4)
    pending["prompt_nonce"] = picker_nonce
    pending["prompt_idx"] = idx

    category_keyboard = InlineKeyboardBuilder()
    categories = item.get("categories", {})
    for category in _cat_order():
        category_meta = _cat_meta().get(category, {})
        name = category_meta.get("name", category)
        category_data = categories.get(category) or {}
        ranges_text = _format_ranges_text(category_data.get("ranges") or [])
        status = ranges_text if category_data.get("ranges") else "не задан"
        category_keyboard.row(InlineKeyboardButton(
            text=f"{category_meta.get('emoji', '❓')} {name} ({status})",
            callback_data=(
                f"yd_cat_choose:{task_id}:{idx}:{picker_nonce}:{category}"
            ),
        ))
    category_keyboard.row(InlineKeyboardButton(
        text="↩️ Назад",
        callback_data=f"yd_cat_edit_back:{task_id}:{idx}:{picker_nonce}",
    ))
    category_keyboard.row(InlineKeyboardButton(
        text="❌ Отменить задачу",
        callback_data=f"yd_task_cancel:{task_id}",
    ))

    try:
        await callback.message.edit_text(
            "✏️ <b>Выберите категорию для изменения диапазона</b>\n\n"
            f"📄 Файл: <code>{file_name}</code>\n"
            f"📊 Всего слайдов: <b>{total_slides}</b>",
            parse_mode="HTML",
            reply_markup=category_keyboard.as_markup(),
        )
    except Exception as error:
        logging.error(
            "Не удалось показать выбор категории для изменения диапазона: %s",
            error,
            exc_info=True,
        )
        await _yd_render_category_prompt(
            task_id=task_id,
            item=item,
            status_msg=callback.message,
        )
        await _safe_answer(callback, "❌ Не удалось открыть редактирование.")
        return

    pending["prompt_message_id"] = callback.message.message_id
    await _restart_prompt_watchdog(task_id, pending, picker_nonce)
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("yd_cat_choose:"))
async def yd_cat_choose(callback: types.CallbackQuery, bot: Bot):
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
    category = parts[4]
    if category not in _cat_order():
        await _safe_answer(callback, "❌ Неизвестная категория.", show_alert=True)
        return

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return
    pending = session["pending"]
    if (
        not isinstance(pending, dict)
        or pending.get("prompt_nonce") != parts[3]
        or pending.get("prompt_idx") != idx
        or callback.from_user.id != pending.get("owner_user_id")
    ):
        await _safe_answer(callback, "⏳ Промпт уже обработан.", show_alert=True)
        return
    if idx < 0 or idx >= len(pending.get("prepared", [])):
        await _safe_answer(callback, "❌ Файл не найден.", show_alert=True)
        return

    pending = await _yd_claim_prompt(callback)
    if pending is None:
        return

    item = pending["prepared"][idx]
    category_data = item.setdefault("categories", {}).setdefault(category, {})
    ranges_text = _format_ranges_text(category_data.get("ranges") or [])
    if not category_data.get("ranges"):
        ranges_text = "не задан"
    category_meta = _cat_meta().get(category, {})
    category_name = html_module.escape(category_meta.get("name", category))
    file_name = html_module.escape(item.get("file_name", ""))
    total_slides = item.get("total_slides", 0)
    manual_nonce = secrets.token_hex(4)

    try:
        await callback.message.edit_text(
            f"✏️ <b>Укажите диапазон: {category_name}</b>\n\n"
            f"📄 Файл: <code>{file_name}</code>\n"
            f"📊 Всего слайдов: <b>{total_slides}</b>\n"
            f"Текущий диапазон: <code>{ranges_text}</code>\n\n"
            f"<b>Формат:</b> <code>5-30</code> или "
            f"<code>5,7,10-15</code>\n"
            "Отправьте диапазон сообщением в чат. Можно указать несколько "
            "диапазонов через запятую.\n"
            "<i>Для возврата без изменений отправьте «отмена».</i>",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as error:
        logging.error(
            "Не удалось показать редактирование категории %s: %s",
            category,
            error,
            exc_info=True,
        )
        await _yd_render_category_prompt(
            task_id=task_id,
            item=item,
            status_msg=callback.message,
        )
        await _safe_answer(callback, "❌ Не удалось открыть редактирование.")
        return

    pending["awaiting_range_for_idx"] = idx
    pending["awaiting_range_category"] = category
    pending["prompt_nonce"] = manual_nonce
    pending["prompt_idx"] = idx
    pending["prompt_message_id"] = callback.message.message_id
    await _restart_prompt_watchdog(task_id, pending, manual_nonce)
    await _safe_answer(callback)


async def _restart_prompt_watchdog(
    task_id: str, pending: dict, expected_nonce: str
) -> None:
    old_timeout = pending.get("prompt_timeout_task")
    if old_timeout is not None and not old_timeout.done():
        old_timeout.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(old_timeout), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            logging.exception("Не удалось дождаться старого prompt watchdog")

    pending["prompt_timeout_task"] = asyncio.create_task(
        _yd_prompt_timeout_watchdog(
            task_id, yandex_state.config.prompt_timeout_sec, expected_nonce
        )
    )
    pending["prompt_watchdog_nonce"] = expected_nonce


@router.callback_query(F.data.startswith("yd_cat_edit_back:"))
async def yd_cat_edit_back(callback: types.CallbackQuery, bot: Bot):
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
    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await _safe_answer(callback, "❌ Сессия неактивна.", show_alert=True)
        return
    pending = session["pending"]
    if (
        not isinstance(pending, dict)
        or pending.get("prompt_nonce") != parts[3]
        or pending.get("prompt_idx") != idx
        or callback.from_user.id != pending.get("owner_user_id")
    ):
        await _safe_answer(callback, "⏳ Промпт уже обработан.", show_alert=True)
        return
    pending = await _yd_claim_prompt(callback)
    if pending is None:
        return
    if idx < 0 or idx >= len(pending.get("prepared", [])):
        await _safe_answer(callback, "❌ Файл не найден.", show_alert=True)
        return
    await _yd_render_category_prompt(
        task_id=task_id,
        item=pending["prepared"][idx],
        status_msg=callback.message,
    )
    await _safe_answer(callback)


# ==========================================
# v4.0: yd_cat_noop (disabled)
# ==========================================

@router.callback_query(F.data.startswith("yd_cat_noop:"))
async def yd_cat_noop(callback: types.CallbackQuery):
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
# Fallback: yd_sermon_mode
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

    if mode == "sermon":
        item["selected_categories"] = {"sermon"}
    elif mode == "other":
        item["selected_categories"] = {"other"}
    else:
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


# ==========================================
# yd_task_cancel
# ==========================================

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
    pending["awaiting_range_category"] = None

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


# ==========================================
# yd_sermon_edit (fallback, только sermon)
# ==========================================

@router.callback_query(F.data.startswith("yd_sermon_edit:"))
async def yd_sermon_edit(callback: types.CallbackQuery, bot: Bot):
    """Старое ручное редактирование диапазона (только для sermon)."""
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
            f"<i>Отправьте <code>отмена</code>, чтобы вернуться "
            f"без изменения диапазона.</i>",
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
    pending["awaiting_range_category"] = "sermon"
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