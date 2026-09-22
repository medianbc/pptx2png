# ==========================================
# yandex_flow.py — ОРКЕСТРАЦИЯ ЯНДЕКС.ДИСКА
# ==========================================

import asyncio
import html as html_module
import logging
import secrets
import shutil
import time
from pathlib import Path
from typing import Optional, List, Tuple

from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import yandex_state
from yandex_state import (
    sessions,
    yd_session_lock,
    yd_active_sessions,
    yd_active_tasks,
    yd_session_key,
    yd_try_acquire,
    yd_release,
    yd_is_active,
    YD_PROMPT_TIMEOUT_SEC,
)

from yandex_disk import (
    YandexDiskError,
    get_nearest_sunday,
    month_folder_name,
    resolve_sunday_paths,
    find_pptx_in_source,
)
from structure import load_template, create_structure, safe_folder_name
from sermon_detector import find_sermon_range
from utils import extract_speaker_notes
from converter_engine import pptx_to_png_conversion  # ← предполагаемая функция


router = Router()


# ==========================================
# УТИЛИТЫ
# ==========================================

def _touch_task(task_dir: Path):
    """Обновляет mtime папки — чтобы cleaner не удалял активную задачу."""
    if task_dir and task_dir.exists():
        try:
            import os
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


def _is_sermon_slide(item: dict, slide_idx: int) -> bool:
    """Проверяет, относится ли слайд к проповеди (по ranges или start..end)."""
    ranges = item.get("ranges")
    if ranges:
        return any(s <= slide_idx <= e for s, e in ranges)
    start = item.get("start")
    end = item.get("end")
    return start is not None and start <= slide_idx <= end


# ==========================================
# ПАЙПЛАЙН ПОДГОТОВКИ
# ==========================================

async def _yd_prepare_files(
    callback: types.CallbackQuery,
    bot: Bot,
    SHM_DIR: str,
    user_mgr,
    converter,
    files_to_process: list,
    sunday,
    paths: dict,
    session_key: str,
    nonce: str,
):
    """
    Первая фаза: скачивание + конвертация всех файлов.
    Сохраняет результат в sessions[task_id]["pending"].
    """
    status_msg = callback.message
    owner_user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    task_id = f"yd_task_{owner_user_id}_{secrets.token_hex(4)}"
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # Создаём минимальную task-сессию СРАЗУ + регистрируем в picker
    async with yd_session_lock:
        picker = sessions.get(session_key)
        if picker is None:
            _safe_delete_task_dir(task_dir)
            try:
                await status_msg.edit_text("❌ Сессия была отменена.")
            except Exception:
                pass
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
        # Загружаем шаблон
        template_path = Path(__file__).parent / yandex_state.config.template_file
        structure = load_template(template_path)
        if structure is None:
            try:
                await status_msg.edit_text(
                    f"❌ <b>Ошибка загрузки template.yaml</b>\n\n"
                    f"Файл: <code>{html_module.escape(str(template_path))}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await _yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
            )
            return

        # Создаём структуру
        try:
            await status_msg.edit_text("📁 Создаю структуру папок...")
        except Exception:
            pass

        structure_base = paths["date_folder"]
        ok = await create_structure(
            yandex_state.config.client, structure_base, structure
        )
        if not ok:
            try:
                await status_msg.edit_text(
                    "❌ <b>Не удалось создать структуру папок</b>\n\n"
                    "Проверьте права на Яндекс.Диске.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await _yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
            )
            return

        target_base = paths["target"]
        quality = user_mgr.get_user_config(owner_user_id)["quality"]

        prepared = []

        for f_idx, pptx_item in enumerate(files_to_process, start=1):
            _touch_task(task_dir)

            task_sess = sessions.get(task_id)
            if task_sess is None or task_sess.get("cancelled"):
                logging.info(f"Задача {task_id} отменена — прерываем подготовку")
                await _yd_cleanup_task(
                    task_id, session_key, task_dir,
                    owner_user_id, chat_id, nonce,
                )
                return

            file_name = pptx_item["name"]
            file_name_esc = html_module.escape(file_name)

            try:
                await status_msg.edit_text(
                    f"📥 Скачиваю <code>{file_name_esc}</code>...",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            local_pptx = task_dir / file_name
            ok = await yandex_state.config.client.download_file(
                pptx_item["path"], local_pptx
            )
            if not ok:
                prepared.append({
                    "file_name": file_name,
                    "file_slug": safe_folder_name(file_name),
                    "failed_at_stage": "download",
                })
                continue

            try:
                await status_msg.edit_text(
                    f"⚙️ Конвертирую <code>{file_name_esc}</code> в PNG...",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            temp_png_dir = task_dir / f"png_{f_idx}"
            temp_png_dir.mkdir(exist_ok=True)

            try:
                pngs, used_pptx = await converter.convert_all_pngs(
                    local_pptx, temp_png_dir, quality
                )
            except Exception as e:
                logging.error(f"Ошибка конвертации {file_name}: {e}", exc_info=True)
                prepared.append({
                    "file_name": file_name,
                    "file_slug": safe_folder_name(file_name),
                    "failed_at_stage": "convert",
                })
                continue

            if not pngs:
                prepared.append({
                    "file_name": file_name,
                    "file_slug": safe_folder_name(file_name),
                    "failed_at_stage": "no_pngs",
                })
                continue

            pngs_sorted = sorted(pngs, key=lambda p: p.name)

            notes_ok, notes, incomplete = await asyncio.to_thread(
                extract_speaker_notes, str(used_pptx)
            )
            if not notes_ok:
                notes = {}
            if used_pptx != local_pptx and used_pptx.exists():
                try:
                    used_pptx.unlink()
                except Exception:
                    pass

            if incomplete:
                start, end, matches = None, None, []
                incomplete_warning = (
                    "⚠️ Заметки прочитаны частично, "
                    "проповедь не определена автоматически."
                )
            else:
                start, end, matches = find_sermon_range(
                    notes, yandex_state.config.sermon_keyword
                )
                if matches and len(matches) == 1:
                    start, end = None, None
                incomplete_warning = None

            item_ranges = [(start, end)] if start is not None else None

            prepared.append({
                "file_name": file_name,
                "file_slug": safe_folder_name(file_name),
                "pngs_sorted": pngs_sorted,
                "start": start,
                "end": end,
                "ranges": item_ranges,
                "matches": matches,
                "incomplete_warning": incomplete_warning,
                "confirmed": start is None,
            })

        # Публикуем pending под локом
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
                    }

        if cleanup_needed:
            logging.info(f"Задача {task_id} отменена до сохранения pending")
            await _yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
            )
            return

        needs_confirm = [
            p for p in prepared
            if "pngs_sorted" in p and p.get("start") is not None and not p.get("confirmed")
        ]

        if not needs_confirm:
            await _yd_upload_files(
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
        logging.error(f"Ошибка _yd_prepare_files: {e}", exc_info=True)
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
    """
    Атомарно проверяет и 'потребляет' промпт.
    Возвращает pending при успехе, иначе None.
    """
    parts = callback.data.split(":")
    if len(parts) != 4:
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
    Обновляет prompt_idx/prompt_nonce под текущий item.
    """
    session = sessions.get(task_id)
    if not session or "pending" not in session:
        return
    if session.get("cancelled"):
        return
    pending = session["pending"]
    if not isinstance(pending, dict):
        return

    idx = pending["prepared"].index(item)
    pending["current_confirm_idx"] = idx
    pending["prompt_idx"] = idx

    if pending.get("prompt_nonce") is None:
        pending["prompt_nonce"] = secrets.token_hex(8)
    prompt_nonce = pending["prompt_nonce"]

    matches = item.get("matches", [])
    start = item.get("start")
    end = item.get("end")
    file_name = item.get("file_name", "")

    preview = ", ".join(str(n) for n in matches[:15])
    if len(matches) > 15:
        preview += f" …и ещё {len(matches) - 15}"

    file_esc = html_module.escape(file_name)
    text = (
        f"🎯 <b>Найдена пометка «проповедь»</b>\n\n"
        f"📄 Файл: <code>{file_esc}</code>\n"
        f"📌 Слайды с пометкой: <code>{preview}</code>\n\n"
        f"📊 Предлагаемый диапазон: <b>{start}–{end}</b>\n\n"
        f"Подтвердите или измените диапазон:"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=f"yd_sermon_ok:{task_id}:{idx}:{prompt_nonce}",
        ),
        InlineKeyboardButton(
            text="✏️ Изменить",
            callback_data=f"yd_sermon_edit:{task_id}:{idx}:{prompt_nonce}",
        ),
        InlineKeyboardButton(
            text="⏭ Пропустить",
            callback_data=f"yd_sermon_skip:{task_id}:{idx}:{prompt_nonce}",
        ),
    )

    sent_msg = None
    if reply_fn is not None:
        sent_msg = await reply_fn(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        sent_msg = status_msg

    # Проверяем, что nonce не перебили
    if pending.get("prompt_nonce") != prompt_nonce:
        logging.info(
            f"_yd_render_sermon_prompt: nonce изменён конкурентно для {task_id}"
        )
        return

    if sent_msg is not None and hasattr(sent_msg, "message_id"):
        pending["prompt_message_id"] = sent_msg.message_id

    # Не пересоздаём watchdog, если он уже стоит для текущего nonce
    existing_task = pending.get("prompt_timeout_task")
    existing_nonce = pending.get("prompt_watchdog_nonce")

    if existing_task is not None and not existing_task.done():
        if existing_nonce == prompt_nonce:
            return
        existing_task.cancel()
        pending["prompt_timeout_task"] = None
        pending["prompt_watchdog_nonce"] = None

    pending["prompt_timeout_task"] = asyncio.create_task(
        _yd_prompt_timeout_watchdog(task_id, YD_PROMPT_TIMEOUT_SEC, prompt_nonce)
    )
    pending["prompt_watchdog_nonce"] = prompt_nonce


async def _yd_prompt_timeout_watchdog(task_id: str, timeout_sec: int, expected_nonce: str):
    """Если пользователь не ответил на промпт — очищаем задачу."""
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
        logging.info(f"⏰ Промпт {task_id} не подтверждён за {timeout_sec}s — очистка")
        await _yd_cleanup_task(
            task_id,
            pending.get("session_key"),
            pending.get("task_dir"),
            pending.get("owner_user_id"),
            pending.get("chat_id"),
            pending.get("nonce"),
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
    """
    Идемпотентная очистка. Безопасна к pending=None и к вызову из собственного watchdog'а.
    """
    # 1. Папка задачи
    try:
        if task_dir and task_dir.exists():
            shutil.rmtree(task_dir)
            logging.info(f"🧹 Удалена папка задачи: {task_dir}")
    except Exception as e:
        logging.error(f"Ошибка удаления task_dir {task_dir}: {e}")

    # 2. Watchdog — устойчиво к pending=None, не отменяем сами себя
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

    # 3. Сессии + реестр активных yd-задач
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

    # 4. yd_release — best effort
    try:
        await yd_release(owner_user_id, chat_id, nonce)
    except Exception as e:
        logging.error(f"Ошибка yd_release для {task_id}: {e}", exc_info=True)

    # 5. Сообщение об ошибке — best effort
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
# ЗАГРУЗКА НА ЯНДЕКС.ДИСК
# ==========================================

async def _yd_upload_files(
    callback: types.CallbackQuery,
    bot: Bot,
    task_id: str,
    status_msg,
):
    session = sessions.get(task_id)
    if not session or "pending" not in session:
        return
    if session.get("cancelled"):
        logging.info(f"Задача {task_id} отменена — upload пропущен")
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

    cleanup_done = False

    try:
        total_uploaded = 0
        total_failed = 0
        report_lines = [f"📁 Обработано файлов: <b>{len(prepared)}</b>\n"]

        for f_idx, item in enumerate(prepared, start=1):
            _touch_task(task_dir)

            current_session = sessions.get(task_id)
            if current_session is None or current_session.get("cancelled"):
                logging.info(f"Задача {task_id} отменена — прерываем upload")
                return

            file_name = item["file_name"]
            file_name_esc = html_module.escape(file_name)

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

            pngs_sorted = item["pngs_sorted"]
            file_slug = item["file_slug"]
            start = item.get("start")
            end = item.get("end")
            incomplete_warning = item.get("incomplete_warning")

            try:
                await status_msg.edit_text(
                    f"📤 Загружаю PNG на Яндекс.Диск ({file_name_esc})...",
                    parse_mode="HTML",
                )
            except Exception as e:
                logging.warning(f"Не удалось обновить status_msg: {e}")

            pptx2png_dir = (
                f"{target_base}/{yandex_state.config.pptx2png_folder}/{file_slug}"
            )
            sermon_dir = f"{target_base}/{yandex_state.config.sermon_folder}"

            ok1 = await yandex_state.config.client.ensure_folder(pptx2png_dir)
            ok2 = await yandex_state.config.client.ensure_folder(sermon_dir)
            if not ok1 or not ok2:
                logging.error(f"Не удалось создать папки: pptx2png={ok1}, sermon={ok2}")
                report_lines.append(f"❌ {file_name_esc} — не удалось создать папки")
                total_failed += 1
                continue

            uploaded_sermon = 0
            uploaded_other = 0
            failed_sermon = 0
            failed_other = 0

            for slide_idx, png_path in enumerate(pngs_sorted, start=1):
                _touch_task(task_dir)

                current_session = sessions.get(task_id)
                if current_session is None or current_session.get("cancelled"):
                    logging.info(f"Задача {task_id} отменена в середине upload")
                    return

                is_sermon = _is_sermon_slide(item, slide_idx)
                if is_sermon:
                    remote_path = f"{sermon_dir}/{file_slug}_{png_path.name}"
                else:
                    remote_path = f"{pptx2png_dir}/{png_path.name}"

                ok = await yandex_state.config.client.upload_file(
                    png_path, remote_path
                )
                if ok:
                    if is_sermon:
                        uploaded_sermon += 1
                    else:
                        uploaded_other += 1
                else:
                    logging.error(f"Не удалось загрузить {png_path.name}")
                    if is_sermon:
                        failed_sermon += 1
                    else:
                        failed_other += 1

            total_uploaded += uploaded_sermon + uploaded_other
            total_failed += failed_sermon + failed_other

            ranges = item.get("ranges")
            if ranges:
                ranges_text = ", ".join(
                    f"{s}–{e}" if s != e else str(s) for s, e in ranges
                )
                sermon_info = (
                    f"🎯 Проповедь ({ranges_text}): "
                    f"{uploaded_sermon} загружено"
                    + (f", {failed_sermon} ошибок" if failed_sermon else "")
                )
                other_info = (
                    f"📄 Остальные: {uploaded_other} загружено"
                    + (f", {failed_other} ошибок" if failed_other else "")
                )
            elif start is not None:
                sermon_info = (
                    f"🎯 Проповедь ({start}–{end}): "
                    f"{uploaded_sermon} загружено"
                    + (f", {failed_sermon} ошибок" if failed_sermon else "")
                )
                other_info = (
                    f"📄 Остальные: {uploaded_other} загружено"
                    + (f", {failed_other} ошибок" if failed_other else "")
                )
            else:
                sermon_info = "🎯 Проповедь: не найдена (все PNG в общую папку)"
                other_info = (
                    f"📄 Все слайды: {uploaded_other} загружено"
                    + (f", {failed_other} ошибок" if failed_other else "")
                )

            entry = (
                f"{f_idx}. 📄 <b>{file_name_esc}</b>\n"
                f"   • {sermon_info}\n"
                f"   • {other_info}"
            )
            if incomplete_warning:
                entry += f"\n   • {incomplete_warning}"
            report_lines.append(entry)

        if total_failed > 0:
            report_lines.append(
                f"\n⚠️ Всего загружено PNG: <b>{total_uploaded}</b>\n"
                f"❌ Ошибок: <b>{total_failed}</b>"
            )
        else:
            report_lines.append(f"\n📊 Всего загружено PNG: <b>{total_uploaded}</b>")

        report_lines.append(
            f"\n🔗 <a href=\"https://disk.yandex.ru/client/disk"
            f"{html_module.escape(pending.get('target_base', ''))}\">"
            f"Открыть на Яндекс.Диске</a>"
        )

        await _yd_send_report(
            bot=bot,
            chat_id=chat_id,
            status_msg=status_msg,
            report_lines=report_lines,
            total_uploaded=total_uploaded,
            total_failed=total_failed,
        )

    except Exception as e:
        logging.error(f"Ошибка _yd_upload_files: {e}", exc_info=True)
        cleanup_done = True
        await _yd_cleanup_task(
            task_id, session_key, task_dir,
            owner_user_id, chat_id, nonce,
            bot=bot, status_msg=status_msg, error=e,
        )
        return
    finally:
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
async def cmd_sunday(message: types.Message, check_access):
    """Проверка Диска + вывод найденных pptx для ближайшего предстоящего воскресенья."""
    if not await check_access(message):
        return

    if yandex_state.config.client is None:
        await message.reply("❌ Яндекс.Диск не настроен. Обратитесь к администратору.")
        return

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
            logging.error(f"Ошибка доступа к источнику: {e}")
            await status_msg.edit_text(
                f"❌ <b>Ошибка обращения к Яндекс.Диску</b>\n\n"
                f"<code>{html_module.escape(str(e))}</code>\n\n"
                f"Попробуйте позже.",
                parse_mode="HTML",
            )
            return

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
                f"Сессия {message.from_user.id}:{message.chat.id} "
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
            f"🔓 Сессия {message.from_user.id}:{message.chat.id} "
            f"освобождена после показа списка (nonce={nonce})"
        )

    except Exception as e:
        logging.error(f"Ошибка cmd_sunday: {e}", exc_info=True)
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
                    f"🔓 Сессия {message.from_user.id}:{message.chat.id} "
                    f"освобождена (неудачный запуск, nonce={nonce})"
                )


@router.callback_query(F.data.startswith("yd_pick:"))
async def yd_pick(
    callback: types.CallbackQuery,
    bot: Bot,
    SHM_DIR: str,
    user_mgr,
    converter,
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
        converter=converter,
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


@router.callback_query(F.data.startswith("yd_sermon_ok:"))
async def yd_sermon_ok(callback: types.CallbackQuery, bot: Bot):
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
    pending["prepared"][idx]["confirmed"] = True

    try:
        await callback.answer("✅ Диапазон подтверждён")
    except Exception as e:
        logging.error(f"yd_sermon_ok: answer failed для {task_id}: {e}", exc_info=True)

    remaining = [
        p for p in pending["prepared"]
        if "pngs_sorted" in p and p.get("start") is not None and not p.get("confirmed")
    ]

    if remaining:
        await _yd_render_sermon_prompt(task_id, remaining[0], callback.message)
    else:
        await _yd_upload_files(
            callback=callback, bot=bot, task_id=task_id, status_msg=callback.message,
        )


@router.callback_query(F.data.startswith("yd_sermon_skip:"))
async def yd_sermon_skip(callback: types.CallbackQuery, bot: Bot):
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
    pending["prepared"][idx]["start"] = None
    pending["prepared"][idx]["end"] = None
    pending["prepared"][idx]["ranges"] = None
    pending["prepared"][idx]["confirmed"] = True

    try:
        await callback.answer("⏭ Пропущено")
    except Exception as e:
        logging.error(f"yd_sermon_skip: answer failed для {task_id}: {e}", exc_info=True)

    remaining = [
        p for p in pending["prepared"]
        if "pngs_sorted" in p and p.get("start") is not None and not p.get("confirmed")
    ]

    if remaining:
        await _yd_render_sermon_prompt(task_id, remaining[0], callback.message)
    else:
        await _yd_upload_files(
            callback=callback, bot=bot, task_id=task_id, status_msg=callback.message,
        )


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
    total_slides = len(current["pngs_sorted"])
    file_name_esc = html_module.escape(current["file_name"])

    # Объединённый guarded-блок: callback.answer + edit_text
    sent_msg = None
    try:
        await callback.answer()
        sent_msg = await callback.message.edit_text(
            f"✏️ <b>Введите диапазон проповеди</b>\n\n"
            f"📄 Файл: <code>{file_name_esc}</code>\n"
            f"📊 Всего слайдов: {total_slides}\n\n"
            f"Пример: <code>5-30</code> или <code>5,7,10-15</code>\n"
            f"Отправьте текстом в чат (ответом на это сообщение).\n"
            f"<i>Отправьте <code>отмена</code> или <code>0</code>, чтобы пропустить.</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(
            f"yd_sermon_edit: callback.answer или edit_text упал для {task_id}: {e}",
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
                    "❌ Не удалось показать форму ввода (кнопка устарела "
                    "или Telegram недоступен). Задача сброшена."
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
            f"yd_sermon_edit: nonce изменён конкурентно для {task_id} — "
            f"watchdog не создаём"
        )
        return

    old_timeout = pending.get("prompt_timeout_task")
    if old_timeout is not None and not old_timeout.done():
        old_timeout.cancel()

    pending["prompt_timeout_task"] = asyncio.create_task(
        _yd_prompt_timeout_watchdog(task_id, YD_PROMPT_TIMEOUT_SEC, manual_nonce)
    )
    pending["prompt_watchdog_nonce"] = manual_nonce


# ==========================================
# ПУБЛИЧНЫЕ ОБЁРТКИ ДЛЯ handle_text_input
# ==========================================
# handle_text_input живёт в handlers.py, но обрабатывает Yandex-промпты.
# Экспортируем нужные функции через эти псевдонимы, чтобы handlers.py не лез в приватные.

render_sermon_prompt = _yd_render_sermon_prompt
upload_files = _yd_upload_files
cleanup_task = _yd_cleanup_task
is_sermon_slide = _is_sermon_slide
claim_prompt = _yd_claim_prompt
