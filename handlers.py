# ==========================================
# handlers.py — ОБРАБОТЧИКИ (v1.8, после рефакторинга)
# ==========================================

import os
import re
import shutil
import logging
import secrets
import asyncio
from pathlib import Path
import html as html_module
from typing import Optional, Dict, List, Tuple

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

# ✅ Yandex-подсистема
import yandex_state
from yandex_state import sessions, yd_session_lock, yd_active_tasks
from yandex_disk import YandexDiskError
from yandex_flow import (
    router as yandex_router,
    render_sermon_prompt,
    cleanup_task as yd_cleanup_task,
    is_sermon_slide,
    prompt_timeout_watchdog,
)

import converter_engine
from converter_engine import convert_all_pngs, create_zip_stream


# ==========================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ (общие)
# ==========================================

router = Router()
converter_semaphore = asyncio.Semaphore(2)

# ✅ Подключаем Yandex-роутер
router.include_router(yandex_router)


# ==========================================
# МЕНЕДЖЕР БЛОКИРОВОК ЗАДАЧ (только для обычной конвертации)
# ==========================================

class TaskLockManager:
    """
    Менеджер блокировок для защиты от дублирующих операций.
    Единственный источник правды — lock.locked().
    Записи удаляются при release(), чтобы не копить мёртвые локи.
    """

    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._dict_lock = asyncio.Lock()

    async def acquire(self, task_id: str) -> bool:
        async with self._dict_lock:
            if task_id not in self._locks:
                self._locks[task_id] = asyncio.Lock()
            lock = self._locks[task_id]
            if lock.locked():
                return False
            await lock.acquire()
            return True

    async def release(self, task_id: str):
        async with self._dict_lock:
            lock = self._locks.get(task_id)
            if lock is not None and lock.locked():
                lock.release()
            # ✅ Bug #2: удаляем запись — не копим мёртвые локи
            self._locks.pop(task_id, None)

    async def is_active(self, task_id: str) -> bool:
        async with self._dict_lock:
            lock = self._locks.get(task_id)
            return lock is not None and lock.locked()

task_lock_manager = TaskLockManager()

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def safe_filename(filename: str) -> str:
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
    if not input_text or not input_text.strip():
        return []
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


def normalize_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    if not ranges:
        return []

    unique_ranges = list(dict.fromkeys(ranges))
    if not unique_ranges:
        return []

    sorted_ranges = sorted(unique_ranges, key=lambda r: r[0])

    merged = []
    start, end = sorted_ranges[0]
    idx = 0

    if end - start + 1 > 1000:
        logging.warning(f"Слишком большой диапазон {start}-{end}, игнорируем.")
        idx = 1
        while idx < len(sorted_ranges):
            s, e = sorted_ranges[idx]
            if e - s + 1 <= 1000:
                start, end = s, e
                break
            logging.warning(f"Слишком большой диапазон {s}-{e}, игнорируем.")
            idx += 1
        else:
            return []

    for next_start, next_end in sorted_ranges[idx + 1:]:
        if next_end - next_start + 1 > 1000:
            logging.warning(f"Слишком большой диапазон {next_start}-{next_end}, игнорируем.")
            continue

        if next_start <= end:
            new_end = max(end, next_end)
            if new_end - start + 1 <= 1000:
                end = new_end
            else:
                merged.append((start, end))
                start, end = next_start, next_end
        else:
            merged.append((start, end))
            start, end = next_start, next_end

    merged.append((start, end))
    return merged


def reset_awaiting_for_user_chat(user_id: int, chat_id: int,
                                 exclude_task_id: Optional[str] = None):
    for tid, sess in sessions.items():
        if sess.get("user_id") == user_id and sess.get("chat_id") == chat_id:
            if exclude_task_id is None or tid != exclude_task_id:
                sess["awaiting_selection"] = False


def get_disabled_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏳ Конвертация...", callback_data="disabled_placeholder"))
    return kb


def touch_task(task_dir: Path):
    if task_dir and task_dir.exists():
        try:
            os.utime(task_dir, None)
        except Exception as e:
            logging.error(f"Ошибка touch для {task_dir}: {e}")


def safe_delete_task_dir(task_dir: Path):
    if task_dir and task_dir.exists():
        try:
            shutil.rmtree(task_dir)
            logging.info(f"🧹 Удалена папка задачи: {task_dir}")
        except Exception as e:
            logging.error(f"Ошибка удаления папки {task_dir}: {e}")


# ==========================================
# КОНТЕКСТНЫЙ МЕНЕДЖЕР ДЛЯ ЗАДАЧИ
# ==========================================

class TaskContext:
    def __init__(self, task_id: str, callback: types.CallbackQuery,
                 SHM_DIR: str, operation: str = "conversion"):
        self.task_id = task_id
        self.callback = callback
        self.SHM_DIR = SHM_DIR
        self.operation = operation
        self.task_dir = None
        self.pptx_path = None
        self.session_data = None
        self.lock_acquired = False

    async def __aenter__(self):
        self.session_data = sessions.get(self.task_id)
        if not self.session_data:
            await self.callback.message.edit_text("❌ Сессия была удалена.")
            raise ValueError("Session not found")

        self.task_dir = Path(self.SHM_DIR) / self.task_id
        if not self.task_dir.exists():
            await self.callback.message.edit_text("❌ Папка задачи удалена.")
            raise FileNotFoundError("Task directory not found")

        self.pptx_path = self.session_data.get("file_path")
        if not self.pptx_path or not Path(self.pptx_path).exists():
            await self.callback.message.edit_text("❌ Файл презентации удален.")
            raise FileNotFoundError("Presentation file not found")

        if not await task_lock_manager.acquire(self.task_id):
            await self.callback.message.edit_text("⏳ Задача уже обрабатывается.")
            raise RuntimeError("Task already processing")
        self.lock_acquired = True

        touch_task(self.task_dir)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.lock_acquired:
            await task_lock_manager.release(self.task_id)
        if self.task_id.startswith("task_"):
            sessions.pop(self.task_id, None)
        safe_delete_task_dir(self.task_dir)


# ==========================================
# ОСНОВНАЯ ФУНКЦИЯ КОНВЕРТАЦИИ
# ==========================================

async def run_conversion(
    callback: types.CallbackQuery,
    task_id: str,
    SHM_DIR: str,
    user_mgr,
    get_settings_keyboard,
    all_slides: bool = True,
    ranges: List[Tuple[int, int]] = None
):
    session = sessions.get(task_id)
    if not session:
        await callback.message.edit_text("❌ Сессия истекла.")
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return

    if callback.from_user.id != session["user_id"] or \
       callback.message.chat.id != session["chat_id"]:
        await callback.message.edit_text("❌ У вас нет доступа к этой задаче.")
        await callback.answer("❌ У вас нет доступа к этой задаче.", show_alert=True)
        return

    async with converter_semaphore:
        try:
            async with TaskContext(task_id, callback, SHM_DIR, "conversion") as ctx:

                cfg = user_mgr.get_user_config(callback.from_user.id)
                chat_id = callback.message.chat.id
                user_id = callback.from_user.id
                pptx_path = ctx.pptx_path

                touch_task(ctx.task_dir)

                if all_slides:
                    expected_zip, final_pdf_path = await core_pipeline(
                        pptx_path, callback.message, user_id, user_mgr
                    )

                    if expected_zip and expected_zip.exists():
                        if expected_zip.stat().st_size > 45 * 1024 * 1024:
                            await callback.message.edit_text("⚠️ **Архив слишком большой (>45 МБ).**")
                            return

                        await callback.message.edit_text("📤 Отправляю готовые файлы...")
                        await callback.bot.send_document(
                            chat_id=chat_id,
                            document=FSInputFile(expected_zip),
                            caption="📦 ZIP со всеми слайдами готов!"
                        )

                        if final_pdf_path and final_pdf_path.exists():
                            await callback.bot.send_document(
                                chat_id=chat_id,
                                document=FSInputFile(final_pdf_path),
                                caption="📄 PDF готов!"
                            )

                        await callback.message.delete()
                    else:
                        await callback.message.edit_text("❌ Ошибка конвертации всех слайдов.")

                elif ranges:
                    final_ranges = normalize_ranges(ranges)

                    if not final_ranges:
                        await callback.message.edit_text("❌ Нет допустимых диапазонов для конвертации.")
                        return

                    temp_png_dir = ctx.task_dir / "temp_pngs"
                    temp_png_dir.mkdir(exist_ok=True)
                    touch_task(ctx.task_dir)

                    all_pngs, _ = await convert_all_pngs(pptx_path, temp_png_dir, cfg["quality"])
                    if _ != pptx_path and _.exists():
                        try:
                            _.unlink()
                        except Exception:
                            pass
                    if not all_pngs:
                        await callback.message.edit_text("❌ Не удалось конвертировать слайды в PNG.")
                        return

                    total_slides = len(all_pngs)
                    archives = []

                    for idx, (start, end) in enumerate(final_ranges):
                        if start > total_slides:
                            await callback.message.edit_text(
                                f"❌ Слайд {start} не существует (всего {total_slides})."
                            )
                            return
                        if end > total_slides:
                            end = total_slides

                        selected = []
                        for i in range(start - 1, end):
                            if i < len(all_pngs):
                                selected.append(all_pngs[i])
                        if not selected:
                            continue

                        range_name = f"slides_{start}-{end}" if start != end else f"slide_{start}"
                        zip_path = ctx.task_dir / f"{pptx_path.stem}_part{idx + 1}_{range_name}.zip"

                        create_zip_stream(selected, zip_path)
                        touch_task(ctx.task_dir)

                        if zip_path.stat().st_size > 45 * 1024 * 1024:
                            zip_path.unlink()
                            await callback.message.edit_text(
                                f"⚠️ **Архив для диапазона {start}-{end} слишком большой (>45 МБ).**"
                            )
                            return
                        archives.append(zip_path)

                    for png_path in all_pngs:
                        if png_path.exists():
                            png_path.unlink()
                    if temp_png_dir.exists():
                        shutil.rmtree(temp_png_dir)

                    if archives:
                        await callback.message.edit_text(f"📤 Отправляю {len(archives)} архив(ов)...")
                        for zip_path in archives:
                            if zip_path.exists():
                                await callback.bot.send_document(
                                    chat_id=chat_id,
                                    document=FSInputFile(zip_path),
                                    caption=f"📦 {zip_path.name}"
                                )
                                zip_path.unlink()
                                touch_task(ctx.task_dir)
                        await callback.message.delete()
                        await callback.bot.send_message(
                            chat_id=chat_id,
                            text="⚙️ **Настройки для следующей презентации:**",
                            reply_markup=get_settings_keyboard(user_id)
                        )
                    else:
                        await callback.message.edit_text("❌ Ошибка создания архивов.")

        except RuntimeError as e:
            if "already processing" in str(e):
                logging.warning(f"Повторный запуск конвертации {task_id}")
                await callback.answer("⏳ Задача уже обрабатывается...", show_alert=True)
            else:
                logging.error(f"RuntimeError run_conversion: {e}")
                await callback.message.edit_text(f"❌ Ошибка: {str(e)[:100]}")
        except ValueError as e:
            logging.error(f"ValueError run_conversion: {e}")
            await callback.message.edit_text(f"❌ Ошибка данных: {str(e)[:100]}")
            await callback.answer("❌ Ошибка данных.", show_alert=True)
        except FileNotFoundError as e:
            logging.error(f"FileNotFound run_conversion: {e}")
            await callback.message.edit_text("❌ Презентация удалена или повреждена.")
            await callback.answer("❌ Презентация не найдена.", show_alert=True)
        except Exception as e:
            logging.error(f"Exception run_conversion: {e}", exc_info=True)
            try:
                await callback.message.edit_text(f"❌ Произошла ошибка: {str(e)[:100]}")
                await callback.answer("❌ Ошибка.", show_alert=True)
            except Exception:
                pass


# ==========================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ПРИВЕТСТВИЯ
# ==========================================

async def send_welcome(message: types.Message, get_settings_keyboard):
    await message.reply(
        "👋 Привет!\n\n"
        "Для начала работы загрузите презентацию в формате **.pptx**, **.ppt** или **.zip**.\n"
        "Также можно отправить google ссылку на файл (доступ на чтение всем).\n\n"
        "⚙️ Настройки качества и PDF:",
        reply_markup=get_settings_keyboard(message.from_user.id)
    )

# ==========================================
# 1. КОМАНДА СТАРТ
# ==========================================

@router.message(CommandStart())
async def cmd_start(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message):
        return

    yd_status = "⚪ Не настроен"
    sunday_line = ""

    if yandex_state.config.client is not None:
        ok, err = await yandex_state.config.client.check_access()
        if ok:
            yd_status = "✅ Доступен"
            from yandex_disk import (
                get_nearest_sunday,
                resolve_sunday_paths,
                find_pptx_in_source,
            )
            sunday = get_nearest_sunday()
            try:
                paths = await resolve_sunday_paths(
                    yandex_state.config.client,
                    yandex_state.config.base_path,
                    sunday,
                    yandex_state.config.source_folder,
                    yandex_state.config.target_folder,
                )
                if paths:
                    pptx_files = await find_pptx_in_source(
                        yandex_state.config.client, paths["source"], sunday
                    )
                    if pptx_files:
                        sunday_line = (
                            f"\n📅 На **{sunday:%d.%m.%Y}** "
                            f"найдено pptx: **{len(pptx_files)}**\n"
                            f"→ /sunday для выбора"
                        )
                    else:
                        sunday_line = f"\n📅 На **{sunday:%d.%m.%Y}** pptx пока нет."
                else:
                    sunday_line = f"\n📅 Структура для **{sunday:%d.%m.%Y}** не найдена."

            # ✅ Bug #1: разделяем ожидаемые и неожиданные ошибки
            except YandexDiskError as e:
                logging.warning(f"Ошибка Яндекс.Диска в /start: {e}")
                yd_status = f"⚠️ {str(e)[:50]}"
            except Exception as e:
                logging.exception(
                    f"Неожиданная ошибка в /start (Yandex): {e}"
                )
                yd_status = "⚠️ Внутренняя ошибка"

        else:
            yd_status = f"❌ {err}"

    await message.reply(
        f"👋 Привет!\n\n"
        f"🟢 Яндекс.Диск: {yd_status}"
        f"{sunday_line}\n\n"
        f"Загрузите презентацию, отправьте ссылку или используйте /sunday.\n\n"
        f"⚙️ Настройки:",
        reply_markup=get_settings_keyboard(message.from_user.id),
    )

# ==========================================
# 2. ВЫБОР СЛАЙДОВ
# ==========================================

@router.callback_query(F.data.startswith("slides_all:"))
async def handle_all_slides(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str,
                            user_mgr, check_access_by_user, get_settings_keyboard):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    if task_id not in sessions:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_disabled_keyboard().as_markup()
        )
    except Exception:
        pass

    await callback.answer("⏳ Начинаю конвертацию...")
    await callback.message.edit_text("⚙️ Запускаю конвертацию всех слайдов...")
    await run_conversion(
        callback, task_id, SHM_DIR, user_mgr,
        get_settings_keyboard, all_slides=True
    )


@router.callback_query(F.data.startswith("slides_select:"))
async def handle_select_slides(callback: types.CallbackQuery, bot: Bot):
    task_id = callback.data.split(":")[-1]
    if task_id not in sessions:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return

    session = sessions[task_id]

    if callback.from_user.id != session.get("user_id") or \
       callback.message.chat.id != session.get("chat_id"):
        await callback.answer("❌ У вас нет доступа к этой задаче.", show_alert=True)
        return

    task_dir = Path(session.get("task_dir", ""))
    if not task_dir.exists():
        sessions.pop(task_id, None)
        await callback.answer("❌ Данные задачи устарели.", show_alert=True)
        await callback.message.edit_text(
            "❌ Данные задачи устарели. Загрузите презентацию заново."
        )
        return

    touch_task(task_dir)
    reset_awaiting_for_user_chat(
        session["user_id"], session["chat_id"], exclude_task_id=task_id
    )

    sessions[task_id]["awaiting_selection"] = True
    await callback.message.edit_text(
        "📝 **Введите номера слайдов для конвертации.** \n\n"
        "Формат ввода:\n"
        "• Отдельные номера: `1, 3, 5, 7` \n"
        "• Диапазоны: `4-12, 15, 20-30` \n"
        "• Смешанный: `1, 3-5, 10, 15-20` \n\n"
        "Если укажете несколько диапазонов, каждый будет упакован в отдельный архив.",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("slides_convert:"))
async def handle_convert_selected(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str,
                                  user_mgr, check_access_by_user, get_settings_keyboard):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    session = sessions.get(task_id)
    if not session:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return

    task_dir = Path(session.get("task_dir", ""))
    if not task_dir.exists():
        sessions.pop(task_id, None)
        await callback.answer("❌ Данные задачи устарели.", show_alert=True)
        await callback.message.edit_text(
            "❌ Данные задачи устарели. Загрузите презентацию заново."
        )
        return

    ranges = session.get("ranges")
    if not ranges:
        await callback.answer("❌ Не выбраны слайды.", show_alert=True)
        return

    touch_task(task_dir)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_disabled_keyboard().as_markup()
        )
    except Exception:
        pass

    await callback.answer("⏳ Начинаю конвертацию...")
    await callback.message.edit_text(f"⚙️ Запускаю конвертацию {len(ranges)} диапазон(ов)...")
    await run_conversion(
        callback, task_id, SHM_DIR, user_mgr,
        get_settings_keyboard, all_slides=False, ranges=ranges
    )


# ==========================================
# 3. ЗАБЛОКИРОВАННАЯ КНОПКА
# ==========================================

@router.callback_query(F.data == "disabled_placeholder")
async def handle_disabled_button(callback: types.CallbackQuery):
    await callback.answer("⏳ Идёт обработка, пожалуйста, подождите...", show_alert=True)


# ==========================================
# 4. ОБРАБОТЧИК ТЕКСТА
# ==========================================

@router.message(F.text & ~F.text.contains("http://") & ~F.text.contains("https://") & ~F.text.startswith("/"))
async def handle_text_input(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message):
        return

    # ============================================================
    # Yandex-ветка: ответ на промпт проповеди
    # ============================================================
    candidates = []
    for tid, sess in sessions.items():
        pending = sess.get("pending")
        if not pending:
            continue
        if pending.get("owner_user_id") != message.from_user.id:
            continue
        if pending.get("chat_id") != message.chat.id:
            continue
        if pending.get("awaiting_range_for_idx") is None:
            continue
        candidates.append((tid, sess, pending))

    target = None
    if message.reply_to_message is not None and message.reply_to_message.from_user.is_bot:
        reply_to_id = message.reply_to_message.message_id
        for tid, sess, pending in candidates:
            if pending.get("prompt_message_id") == reply_to_id:
                target = (tid, sess, pending)
                break
    elif len(candidates) == 1:
        target = candidates[0]
    elif len(candidates) > 1:
        await message.reply(
            "⚠️ У вас несколько активных задач. Ответьте реплаем на нужное сообщение с промптом."
        )
        return

    if target is not None:
        tid, sess, pending = target

        if sess.get("cancelled"):
            await message.reply("❌ Задача была отменена.")
            return

        idx = pending.get("awaiting_range_for_idx")
        current = pending["prepared"][idx]

        # total_slides — считаем из PPTX
        file_path = current.get("file_path")
        total_slides = 0
        if file_path and Path(file_path).exists():
            try:
                from pptx import Presentation
                prs = Presentation(str(file_path))
                total_slides = len(prs.slides._sldIdLst)
            except Exception as e:
                logging.warning(f"Не удалось определить число слайдов: {e}")

        text_clean = message.text.strip().lower()

        # "отмена"/"0" = пользователь не хочет вводить диапазон
        if text_clean in ("отмена", "cancel", "0"):
            pending.pop("awaiting_range_for_idx", None)

            timeout_task = pending.get("prompt_timeout_task")
            if timeout_task is not None and not timeout_task.done():
                timeout_task.cancel()
            pending["prompt_timeout_task"] = None
            pending["prompt_watchdog_nonce"] = None
            pending["prompt_nonce"] = None
            pending["prompt_idx"] = None
            pending["prompt_message_id"] = None

            await message.reply(
                "⏭ Хорошо, диапазон не меняем. Показываю варианты ещё раз."
            )
            await render_sermon_prompt(
                task_id=tid,
                item=current,
                status_msg=None,
                reply_fn=message.reply,
            )
            return

        # Парсим ввод
        ranges = parse_slides_ranges(message.text.strip())
        if not ranges:
            old_timeout = pending.get("prompt_timeout_task")
            if old_timeout is not None and not old_timeout.done():
                old_timeout.cancel()
            pending["prompt_timeout_task"] = None
            pending["prompt_watchdog_nonce"] = None
            if pending.get("prompt_nonce") is not None:
                current_nonce = pending["prompt_nonce"]
                pending["prompt_timeout_task"] = asyncio.create_task(
                    prompt_timeout_watchdog(
                        tid,
                        yandex_state.config.prompt_timeout_sec,
                        current_nonce,
                    )
                )
                pending["prompt_watchdog_nonce"] = current_nonce

            await message.reply(
                "❌ **Неверный формат.** Пример: `5-30` или `5,7,10-15`"
            )
            return

        # Клиппинг ДО нормализации
        clipped = []
        warning_parts = []
        for s, e in ranges:
            if s > total_slides:
                warning_parts.append(f"слайд {s} не существует — пропущен")
                continue
            if e > total_slides:
                warning_parts.append(
                    f"диапазон {s}–{e} сокращён до {s}–{total_slides}"
                )
                e = total_slides
            clipped.append((s, e))

        normalized = normalize_ranges(clipped) if clipped else []
        if not normalized and clipped:
            normalized = clipped

        warning = ("\n⚠️ " + "; ".join(warning_parts)) if warning_parts else ""

        # Сохраняем диапазон в prepared
        current["ranges"] = normalized
        current["start"] = normalized[0][0] if normalized else None
        current["end"] = normalized[-1][1] if normalized else None

        # ✅ Bug #4: заполняем matches всеми номерами слайдов из диапазона.
        # Это нужно, чтобы _yd_render_sermon_prompt увидел has_valid_range=True
        # и показал все 3 кнопки режимов (sermon / other / both).
        if normalized:
            all_matches = []
            for s, e in normalized:
                all_matches.extend(range(s, e + 1))
            current["matches"] = sorted(set(all_matches))
            logging.debug(
                f"[YD-INPUT] Ручной диапазон: ranges={normalized}, "
                f"matches={current['matches'][:20]}"
                + ("..." if len(current["matches"]) > 20 else "")
            )
        else:
            current["matches"] = []

        # ✅ Помечаем, что диапазон был введён вручную — пригодится в промпте
        current["manual_range"] = True
        current["confirmed"] = False
        pending.pop("awaiting_range_for_idx", None)

        # Отменяем watchdog
        timeout_task = pending.get("prompt_timeout_task")
        if timeout_task is not None and not timeout_task.done():
            timeout_task.cancel()
        pending["prompt_timeout_task"] = None
        pending["prompt_watchdog_nonce"] = None
        pending["prompt_nonce"] = None
        pending["prompt_idx"] = None
        pending["prompt_message_id"] = None

        # Уведомляем о принятом диапазоне
        if normalized:
            ranges_text = ", ".join(
                f"{s}–{e}" if s != e else str(s) for s, e in normalized
            )
            await message.reply(
                f"✅ Диапазон установлен: <b>{ranges_text}</b>{warning}",
                parse_mode="HTML",
            )
        else:
            await message.reply(
                f"⚠️ Ни один слайд не попал в диапазон.{warning}",
                parse_mode="HTML",
            )

        # ✅ Показываем промпт с режимами конвертации
        await render_sermon_prompt(
            task_id=tid,
            item=current,
            status_msg=None,
            reply_fn=message.reply,
        )
        return

    # ============================================================
    # Обычная конвертация (не Yandex)
    # ============================================================
    user_id = message.from_user.id
    target_chat_id = message.chat.id

    active_session = None
    active_task_id = None

    for tid, sess in sessions.items():
        if sess.get("user_id") == user_id and \
           sess.get("chat_id") == target_chat_id and \
           sess.get("awaiting_selection"):
            active_session = sess
            active_task_id = tid
            break

    if not active_session:
        await send_welcome(message, get_settings_keyboard)
        return

    ranges = parse_slides_ranges(message.text.strip())
    if not ranges:
        await message.reply(
            "❌ **Неверный формат.**\n\nПримеры: `1, 3, 5, 7` или `4-12, 15, 20-30`"
        )
        return

    task_dir = Path(active_session.get("task_dir", ""))
    touch_task(task_dir)

    active_session["ranges"] = ranges
    active_session["awaiting_selection"] = False

    ranges_text = ", ".join(
        [f"{r[0]}-{r[1]}" if r[0] != r[1] else str(r[0]) for r in ranges]
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="✅ Конвертировать",
            callback_data=f"slides_convert:{active_task_id}",
        ),
        InlineKeyboardButton(
            text="✏️ Изменить",
            callback_data=f"slides_select:{active_task_id}",
        ),
    )
    await message.reply(
        f"📊 **Вы выбрали:** {ranges_text}\n\n"
        f"{len(ranges)} архив(ов).\nНажмите 'Конвертировать'.",
        parse_mode="Markdown",
        reply_markup=kb.as_markup(),
    )


# ==========================================
# 5. СПЕЛЛЕР
# ==========================================

@router.callback_query(F.data.startswith("chk_spell:"))
async def callback_run_speller(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str,
                               check_access_by_user):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]

    if not await task_lock_manager.acquire(task_id):
        await callback.answer("⏳ Задача уже обрабатывается.", show_alert=True)
        return

    try:
        task_dir, pptx_path = await _validate_task_ownership(callback, task_id, SHM_DIR)
        if not task_dir or not pptx_path:
            return

        touch_task(task_dir)

        disabled_kb = InlineKeyboardBuilder()
        disabled_kb.row(InlineKeyboardButton(
            text="⏳ Обработка...",
            callback_data=f"disabled_{task_id}"
        ))
        await callback.message.edit_reply_markup(reply_markup=disabled_kb.as_markup())
        await callback.message.edit_text("🔍 Извлекаю текст и отправляю в Яндекс.Спеллер...")

        extract_success, slides_text = await asyncio.to_thread(
            extract_text_from_pptx, str(pptx_path)
        )

        if not extract_success:
            await callback.message.edit_text(
                "❌ **Не удалось извлечь текст из презентации.**\n\n"
                "Вы можете продолжить конвертацию без проверки орфографии:",
                parse_mode="Markdown"
            )
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(
                text="⚙️ Конвертировать",
                callback_data=f"chk_conv:{task_id}"
            ))
            await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
            await callback.answer()
            return

        check_success, spelling_result = await check_spelling(slides_text)

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(
            text="⚙️ Всё равно конвертировать",
            callback_data=f"chk_conv:{task_id}"
        ))

        if not check_success:
            await callback.message.edit_text(
                f"{spelling_result}\n\nВы можете продолжить конвертацию без проверки орфографии:",
                parse_mode="HTML", reply_markup=kb.as_markup()
            )
        else:
            await callback.message.edit_text(
                spelling_result, parse_mode="HTML", reply_markup=kb.as_markup()
            )

        await callback.answer()

    except Exception as e:
        logging.error(f"Ошибка callback_run_speller: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка при проверке.", show_alert=True)
    finally:
        await task_lock_manager.release(task_id)


# ==========================================
# 6. СТАРАЯ КОНВЕРТАЦИЯ
# ==========================================

@router.callback_query(F.data.startswith("chk_conv:"))
async def callback_run_conversion(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str,
                                  user_mgr, check_access_by_user, get_settings_keyboard):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    task_id = callback.data.split(":")[-1]
    if task_id not in sessions:
        await callback.answer("❌ Сессия истекла.", show_alert=True)
        return

    session = sessions[task_id]
    task_dir = Path(session.get("task_dir", ""))
    touch_task(task_dir)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_disabled_keyboard().as_markup()
        )
    except Exception:
        pass

    await callback.answer("⏳ Начинаю конвертацию...")
    await callback.message.edit_text("⚙️ Запускаю конвертацию...")
    await run_conversion(
        callback, task_id, SHM_DIR, user_mgr,
        get_settings_keyboard, all_slides=True
    )


# ==========================================
# 7. ПРОВЕРКА ВЛАДЕЛЬЦА ЗАДАЧИ
# ==========================================

async def _validate_task_ownership(callback: types.CallbackQuery, task_id: str,
                                   SHM_DIR: str) -> tuple:
    task_dir = Path(SHM_DIR) / task_id
    ownership_file = task_dir / ".owner"
    if not task_dir.exists():
        await callback.answer("❌ Срок действия сессии истек.", show_alert=True)
        return None, None
    if not ownership_file.exists():
        await callback.answer("❌ Данные задачи повреждены.", show_alert=True)
        return None, None
    try:
        owner_data = ownership_file.read_text().strip()
        owner_user_id, owner_chat_id = map(int, owner_data.split(":"))
    except Exception:
        await callback.answer("❌ Ошибка чтения данных задачи.", show_alert=True)
        return None, None
    if callback.from_user.id != owner_user_id:
        await callback.answer("❌ Эта задача принадлежит другому пользователю.", show_alert=True)
        return None, None
    if callback.message.chat.id != owner_chat_id:
        await callback.answer("❌ Эта задача создана в другом чате.", show_alert=True)
        return None, None
    pptx_path = next(task_dir.glob("*.pptx"), None)
    if not pptx_path:
        await callback.answer("❌ Файл презентации не найден.", show_alert=True)
        return None, None
    return task_dir, pptx_path


# ==========================================
# 8. АДМИНСКИЕ ХЕНДЛЕРЫ
# ==========================================

@router.callback_query(F.data.startswith("adm_"))
async def handle_admin_decision(callback: types.CallbackQuery, user_mgr, bot: Bot,
                                ADMIN_ID: int):
    if callback.from_user.id != ADMIN_ID:
        return
    data = callback.data.split("_")
    action, target_id = data[1], int(data[2])
    if action == "allow":
        user_mgr.save_allowed_user(target_id)
        await callback.message.edit_text(f"✅ Доступ для `{target_id}` одобрен.")
        try:
            await bot.send_message(target_id, "🎉 Доступ одобрен! Нажмите /start.")
        except Exception:
            pass
    elif action == "deny":
        await callback.message.edit_text(f"❌ Запрос `{target_id}` отклонен.")
        try:
            await bot.send_message(target_id, "❌ Доступ отклонен.")
        except Exception:
            pass
    await callback.answer()


# ==========================================
# 9. НАСТРОЙКИ КАЧЕСТВА И PDF
# ==========================================

@router.callback_query(F.data.startswith("set_q_"))
async def handle_quality_settings(callback: types.CallbackQuery, user_mgr,
                                  get_settings_keyboard, check_access_by_user, bot: Bot):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    user_id = callback.from_user.id
    new_quality = callback.data.replace("set_q_", "")
    user_mgr.update_user_config(user_id, "quality", new_quality)
    try:
        await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
        await callback.answer(f"Качество обновлено: {new_quality.upper()}")
    except Exception as e:
        logging.error(f"Error updating quality keyboard: {e}")
        await callback.answer("❌ Ошибка обновления качества", show_alert=True)


@router.callback_query(F.data == "toggle_pdf")
async def handle_toggle_pdf(callback: types.CallbackQuery, user_mgr,
                            get_settings_keyboard, check_access_by_user, bot: Bot):
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    user_id = callback.from_user.id
    current_config = user_mgr.get_user_config(user_id)
    new_pdf_status = not current_config.get("keep_pdf", False)
    user_mgr.update_user_config(user_id, "keep_pdf", new_pdf_status)
    try:
        await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
        status_text = "Да (ZIP + PDF)" if new_pdf_status else "Нет (Только ZIP)"
        await callback.answer(f"PDF: {status_text}")
    except Exception as e:
        logging.error(f"Error toggling PDF keyboard: {e}")
        await callback.answer("❌ Ошибка обновления PDF", show_alert=True)


# ==========================================
# 10. ОБРАБОТЧИКИ ФАЙЛОВ
# ==========================================

@router.message(F.document.file_name.lower().endswith(('.pptx', '.ppt')))
async def handle_pptx_document(message: types.Message, bot: Bot, SHM_DIR: str, check_access):
    if not await check_access(message):
        return
    document = message.document
    user_id = message.from_user.id
    chat_id = message.chat.id

    safe_name = safe_filename(document.file_name)
    if not safe_name.lower().endswith(('.pptx', '.ppt')):
        await message.reply("❌ Неверный формат.")
        return

    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / ".owner").write_text(f"{user_id}:{chat_id}")

    file_path = task_dir / safe_name
    if not validate_download_path(task_dir, file_path):
        await message.reply("❌ Ошибка безопасности.")
        return

    status_msg = await message.reply("⏳ Скачиваю презентацию...")
    success = False

    try:
        file_info = await bot.get_file(document.file_id)
        await bot.download_file(file_info.file_path, destination=file_path)

        reset_awaiting_for_user_chat(user_id, chat_id)
        sessions[task_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "task_dir": task_dir,
            "file_path": file_path,
            "awaiting_selection": True,
            "ranges": []
        }
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        await status_msg.edit_text(
            f"📄 **Файл '{safe_name}' загружен.**\n\n"
            "Вы можете сразу ввести номера слайдов в чат или выбрать вариант ниже:",
            parse_mode="Markdown", reply_markup=kb.as_markup()
        )
        success = True
        touch_task(task_dir)

    except Exception as e:
        logging.error(f"Ошибка загрузки: {e}")
        try:
            await status_msg.edit_text("❌ Ошибка загрузки.")
        except Exception:
            pass
        if task_id in sessions:
            sessions.pop(task_id, None)
        safe_delete_task_dir(task_dir)
        raise
    finally:
        if not success and task_id in sessions:
            sessions.pop(task_id, None)
        if not success:
            safe_delete_task_dir(task_dir)


@router.message(F.document)
async def handle_docs(message: types.Message, bot: Bot, SHM_DIR: str, check_access,
                      user_mgr, get_settings_keyboard):
    if not await check_access(message):
        return
    safe_name = safe_filename(message.document.file_name)
    ext = Path(safe_name).suffix.lower()
    if ext not in ['.zip', '.pptx', '.ppt']:
        await message.reply("❌ Поддерживаются только PPTX, PPT и ZIP.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(exist_ok=True)
    (task_dir / ".owner").write_text(f"{user_id}:{chat_id}")

    file_path = task_dir / safe_name
    if not validate_download_path(task_dir, file_path):
        await message.reply("❌ Ошибка безопасности.")
        return

    status_msg = await message.reply("📥 Загрузка...")
    success = False

    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, destination=file_path)

        if not file_path.exists() or file_path.stat().st_size == 0:
            await status_msg.edit_text("❌ Пустой файл.")
            return

        if ext == '.zip':
            pptx_path = converter_engine.extract_zip_if_needed(file_path, task_dir)
            if not pptx_path:
                await status_msg.edit_text("❌ В ZIP нет презентации.")
                return
            file_path = pptx_path

        reset_awaiting_for_user_chat(user_id, chat_id)
        sessions[task_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "task_dir": task_dir,
            "file_path": file_path,
            "awaiting_selection": True,
            "ranges": []
        }
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        await status_msg.edit_text(
            f"📄 **Файл '{safe_name}' загружен.**\n\n"
            "Вы можете сразу ввести номера слайдов в чат или выбрать вариант ниже:",
            parse_mode="Markdown", reply_markup=kb.as_markup()
        )
        success = True
        touch_task(task_dir)

    except Exception as e:
        logging.error(f"Ошибка загрузки ZIP: {e}")
        try:
            await status_msg.edit_text("❌ Ошибка обработки архива.")
        except Exception:
            pass
        if task_id in sessions:
            sessions.pop(task_id, None)
        safe_delete_task_dir(task_dir)
        raise
    finally:
        if not success and task_id in sessions:
            sessions.pop(task_id, None)
        if not success:
            safe_delete_task_dir(task_dir)


# ==========================================
# 11. ОБРАБОТЧИК ССЫЛОК
# ==========================================

@router.message(F.text.contains("http://") | F.text.contains("https://"))
async def handle_links(message: types.Message, bot: Bot, SHM_DIR: str, check_access):
    if not await check_access(message):
        return

    url = converter_engine.convert_to_direct_download(message.text.strip())
    user_id = message.from_user.id
    chat_id = message.chat.id
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(exist_ok=True)
    (task_dir / ".owner").write_text(f"{user_id}:{chat_id}")

    file_path = task_dir / "downloaded_presentation.pptx"
    status_msg = await message.reply("🌐 Скачивание ссылки...")
    success = False

    try:
        if "disk.yandex" in url or "yadi.sk" in url:
            await status_msg.edit_text("🌐 Скачивание с Яндекс.Диска...")
            download_success = await download_yandex_disk(url, file_path)
        else:
            direct_url = converter_engine.convert_to_direct_download(url)
            download_success = await download_file_by_url(direct_url, file_path, status_msg)

        if not download_success:
            await status_msg.edit_text("❌ Не удалось скачать файл по ссылке.")
            return

        reset_awaiting_for_user_chat(user_id, chat_id)
        sessions[task_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "task_dir": task_dir,
            "file_path": file_path,
            "awaiting_selection": True,
            "ranges": []
        }

        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📊 Все слайды", callback_data=f"slides_all:{task_id}"),
            InlineKeyboardButton(text="📝 Выбрать слайды", callback_data=f"slides_select:{task_id}")
        )
        await status_msg.edit_text(
            "📄 **Файл загружен по ссылке.**\n\n"
            "Вы можете сразу ввести номера слайдов в чат или выбрать вариант ниже:",
            reply_markup=kb.as_markup()
        )
        success = True
        touch_task(task_dir)

    except Exception as e:
        logging.error(f"Ошибка в handle_links: {e}")
        try:
            await status_msg.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass
        if task_id in sessions:
            sessions.pop(task_id, None)
        safe_delete_task_dir(task_dir)
        raise
    finally:
        if not success and task_id in sessions:
            sessions.pop(task_id, None)
        if not success:
            safe_delete_task_dir(task_dir)
