# ==========================================
# handlers.py — ОБРАБОТЧИКИ (v1.2) — часть 1/6
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
        """Захватить блокировку. True — успешно, False — уже занято."""
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
        """Освободить блокировку."""
        async with self._dict_lock:
            self._active.discard(task_id)
            if task_id in self._locks:
                lock = self._locks[task_id]
                if lock.locked():
                    lock.release()
                self._locks.pop(task_id, None)

    async def is_active(self, task_id: str) -> bool:
        """Проверить, активна ли задача."""
        async with self._dict_lock:
            return task_id in self._active


task_lock_manager = TaskLockManager()
# ==========================================
# handlers.py — ОБРАБОТЧИКИ (v1.2) — часть 2/6
# ==========================================

# (продолжение файла)

def safe_filename(filename: str) -> str:
    """Приводит имя файла к безопасному виду."""
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
    """Проверяет, что destination находится внутри task_dir."""
    try:
        return destination.resolve().parent == task_dir.resolve() or \
               destination.resolve().parent in task_dir.resolve().parents
    except Exception:
        return False


def generate_task_id(chat_id: int, user_id: int, message_id: int) -> str:
    """Генерирует уникальный идентификатор задачи."""
    return f"task_{chat_id}_{user_id}_{message_id}_{secrets.token_hex(8)}"


def parse_slides_ranges(input_text: str) -> List[Tuple[int, int]]:
    """Парсит диапазоны слайдов из текста (1-3, 5, 7-10)."""
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


def reset_awaiting_for_user_chat(user_id: int, chat_id: int,
                                 exclude_task_id: Optional[str] = None):
    """Сбрасывает awaiting_selection у всех сессий пары (user, chat)."""
    for tid, sess in sessions.items():
        if sess.get("user_id") == user_id and sess.get("chat_id") == chat_id:
            if exclude_task_id is None or tid != exclude_task_id:
                sess["awaiting_selection"] = False


def get_disabled_keyboard() -> InlineKeyboardBuilder:
    """Клавиатура с заблокированной кнопкой."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏳ Конвертация...", callback_data="disabled_placeholder"))
    return kb


def touch_task(task_dir: Path):
    """
    Обновляет время модификации папки задачи.
    Нужно, чтобы очистка не удаляла активные задачи.
    """
    if task_dir and task_dir.exists():
        try:
            os.utime(task_dir, None)
        except Exception as e:
            logging.error(f"Ошибка touch для {task_dir}: {e}")


# ==========================================
# НОРМАЛИЗАЦИЯ ДИАПАЗОНОВ
# ==========================================

def normalize_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """
    Объединяет ТОЛЬКО перекрывающиеся диапазоны (размер <= 1000).
    Соседние (1-3, 4-6) остаются отдельными.
    Точные дубликаты удаляются.
    """
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


def safe_delete_task_dir(task_dir: Path):
    """Безопасно удаляет папку задачи."""
    if task_dir and task_dir.exists():
        try:
            shutil.rmtree(task_dir)
            logging.info(f"🧹 Удалена папка задачи: {task_dir}")
        except Exception as e:
            logging.error(f"Ошибка удаления папки {task_dir}: {e}")

# ==========================================
# handlers.py — ОБРАБОТЧИКИ (v1.2) — часть 3/6
# ==========================================

# (продолжение файла)

class TaskContext:
    """Контекстный менеджер задачи конвертации."""

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
        # 1. Проверяем сессию
        self.session_data = sessions.get(self.task_id)
        if not self.session_data:
            await self.callback.message.edit_text("❌ Сессия была удалена.")
            raise ValueError("Session not found")

        # 2. Проверяем файлы ДО блокировки
        self.task_dir = Path(self.SHM_DIR) / self.task_id
        if not self.task_dir.exists():
            await self.callback.message.edit_text("❌ Папка задачи удалена.")
            raise FileNotFoundError("Task directory not found")

        self.pptx_path = self.session_data.get("file_path")
        if not self.pptx_path or not Path(self.pptx_path).exists():
            await self.callback.message.edit_text("❌ Файл презентации удален.")
            raise FileNotFoundError("Presentation file not found")

        # 3. Захват блокировки
        if not await task_lock_manager.acquire(self.task_id):
            await self.callback.message.edit_text("⏳ Задача уже обрабатывается.")
            raise RuntimeError("Task already processing")
        self.lock_acquired = True

        # 4. Обновляем mtime
        touch_task(self.task_dir)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.lock_acquired:
            await task_lock_manager.release(self.task_id)
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

                    all_pngs = await convert_all_pngs(pptx_path, temp_png_dir, cfg["quality"])
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
# handlers.py — ОБРАБОТЧИКИ (v1.2) — часть 4/6
# ==========================================

# (продолжение файла)

async def convert_all_pngs(pptx_path: Path, output_dir: Path, quality: str) -> List[Path]:
    """Конвертирует PPTX → PNG в фоновом потоке."""
    def _sync_convert():
        if pptx_path.suffix.lower() == '.ppt':
            pptx_converted = converter_engine.ppt_to_pptx_crossplatform(pptx_path, output_dir)
        else:
            pptx_converted = pptx_path

        temp_dark_pptx = output_dir / f"temp_dark_{pptx_converted.name}"
        make_dark_mode(pptx_converted, temp_dark_pptx)

        pdf_path = converter_engine.pptx_to_pdf_crossplatform(temp_dark_pptx, output_dir)
        total_slides, png_paths = converter_engine.pdf_to_png_fast(pdf_path, output_dir, quality)

        if pdf_path.exists():
            pdf_path.unlink()
        if temp_dark_pptx.exists():
            temp_dark_pptx.unlink()
        if pptx_converted != pptx_path and pptx_converted.exists():
            pptx_converted.unlink()

        return png_paths

    return await asyncio.to_thread(_sync_convert)


def create_zip_stream(file_paths: List[Path], output_path: Path) -> Path:
    """Создаёт ZIP-архив из списка файлов."""
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for fpath in file_paths:
            if fpath.exists():
                zf.write(fpath, arcname=fpath.name)
    return output_path


# ==========================================
# ПРИВЕТСТВИЕ
# ==========================================

async def send_welcome(message: types.Message, get_settings_keyboard):
    """Отправляет приветственное сообщение с настройками."""
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

    if yandex_client is not None:
        ok, err = await yandex_client.check_access()
        if ok:
            yd_status = "✅ Доступен"
            sunday = get_nearest_sunday()
            try:
                paths = await resolve_sunday_paths(
                    yandex_client, yandex_base_path, sunday,
                    yandex_source_folder, yandex_target_folder,
                )
                if paths:
                    pptx_files = await find_pptx_in_source(
                        yandex_client, paths["source"], sunday
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
            except YandexDiskError as e:
                logging.error(f"Ошибка Яндекс.Диска в /start: {e}")
                yd_status = f"⚠️ {str(e)[:50]}"
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
# 2. ВЫБОР СЛАЙДОВ (callback)
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
# handlers.py — ОБРАБОТЧИКИ (v1.2) — часть 5/6
# ==========================================

# (продолжение файла)

@router.callback_query(F.data == "disabled_placeholder")
async def handle_disabled_button(callback: types.CallbackQuery):
    """Обработчик нажатия на заблокированную кнопку."""
    await callback.answer("⏳ Идёт обработка, пожалуйста, подождите...", show_alert=True)


# ==========================================
# 4. ОБРАБОТЧИК ТЕКСТА (ввод диапазонов)
# ==========================================

@router.message(F.text & ~F.text.contains("http://") & ~F.text.contains("https://") & ~F.text.startswith("/"))
async def handle_text_input(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message):
        return
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
        InlineKeyboardButton(text="✅ Конвертировать", callback_data=f"slides_convert:{active_task_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"slides_select:{active_task_id}")
    )
    await message.reply(
        f"📊 **Вы выбрали:** {ranges_text}\n\n{len(ranges)} архив(ов).\nНажмите 'Конвертировать'.",
        parse_mode="Markdown", reply_markup=kb.as_markup()
    )


# ==========================================
# 5. СПЕЛЛЕР (с блокировкой)
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

        from utils import extract_text_from_pptx, check_spelling
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
# 6. СТАРАЯ КОНВЕРТАЦИЯ (из спеллера)
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
        await callback.message.edit_reply_m

