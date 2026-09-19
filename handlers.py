# ==========================================
# handlers.py — ОБРАБОТЧИКИ (v1.3, исправленная версия)
# ==========================================

import os
import shutil
import logging
import secrets
import asyncio
import zipfile
import time
from pathlib import Path
import html as html_module
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
    extract_speaker_notes,
)
from structure import load_template, create_structure, safe_folder_name
from sermon_detector import find_sermon_range, format_sermon_message

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
# {user_id:chat_id -> nonce (generation)}
yd_active_sessions: Dict[str, str] = {}


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
    Если передан nonce — снимает только при совпадении (защита от отмены чужих сессий).
    Возвращает True, если блокировка была снята (или уже отсутствовала с совпадающим nonce).
    """
    key = yd_session_key(user_id, chat_id)
    async with yd_session_lock:
        current = yd_active_sessions.get(key)
        if current is None:
            return True  # уже нет — нечего освобождать
        if nonce is not None and current != nonce:
            return False  # чужая сессия — не трогаем
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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

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
# КОНТЕКСТНЫЙ МЕНЕДЖЕР ДЛЯ ЗАДАЧИ
# ==========================================

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

                    all_pngs, _ = await convert_all_pngs(pptx_path, temp_png_dir, cfg["quality"])
                    # также в конце удалить временный .pptx
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
# КОНВЕРТАЦИЯ В PNG
# ==========================================

async def convert_all_pngs(pptx_path: Path, output_dir: Path, quality: str) -> Tuple[List[Path], Path]:
    """
    Конвертирует PPTX → PNG.
    Возвращает (список PNG, путь к .pptx, использованному для рендера).
    Для .ppt — путь к временно сконвертированному .pptx (уже удалён).
    """
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
        # ❌ НЕ удаляем pptx_converted здесь — он нам ещё нужен для заметок
        # если это .ppt — удалим после заметок

        return png_paths, pptx_converted

    pngs, used_pptx = await asyncio.to_thread(_sync_convert)
    return pngs, used_pptx


# ==========================================
# ПОТОКОВОЕ СОЗДАНИЕ ZIP
# ==========================================

def create_zip_stream(file_paths: List[Path], output_path: Path) -> Path:
    """Создаёт ZIP-архив из списка файлов."""
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for fpath in file_paths:
            if fpath.exists():
                zf.write(fpath, arcname=fpath.name)
    return output_path


# ==========================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ПРИВЕТСТВИЯ
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
# 3. ЗАБЛОКИРОВАННАЯ КНОПКА
# ==========================================

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

    # ✅ Проверка: не ждём ли мы ручной ввод диапазона проповеди?
    for tid, sess in sessions.items():
        pending = sess.get("pending")
        if not pending:
            continue
        if pending.get("owner_user_id") != message.from_user.id:
            continue
        if pending.get("chat_id") != message.chat.id:
            continue
        idx = pending.get("awaiting_range_for_idx")
        if idx is None:
            continue

        # Это ответ пользователя на "Введите диапазон"
        current = pending["prepared"][idx]
        total_slides = len(current["pngs_sorted"])

        ranges = parse_slides_ranges(message.text.strip())
        if not ranges:
            await message.reply(
                "❌ **Неверный формат.** Пример: `5-30` или `5,7,10-15`"
            )
            return

        # ✅ Сохраняем нормализованный список диапазонов
        normalized = normalize_ranges(ranges)

        # Обрезаем каждый диапазон по total_slides
        clipped = []
        warning_parts = []
        for s, e in normalized:
            if s > total_slides:
                warning_parts.append(f"слайд {s} не существует — пропущен")
                continue
            if e > total_slides:
                warning_parts.append(f"диапазон {s}–{e} сокращён до {s}–{total_slides}")
                e = total_slides
            clipped.append((s, e))

        # ✅ Формируем предупреждение
        warning = ("\n⚠️ " + "; ".join(warning_parts)) if warning_parts else ""

        current["ranges"] = clipped
        current["start"] = clipped[0][0] if clipped else None
        current["end"] = clipped[-1][1] if clipped else None
        current["confirmed"] = True
        pending.pop("awaiting_range_for_idx", None)

        # Сообщение пользователю
        if clipped:
            ranges_text = ", ".join(
                f"{s}–{e}" if s != e else str(s) for s, e in clipped
            )
            await message.reply(
                f"✅ Диапазон установлен: <b>{ranges_text}</b>{warning}",
                parse_mode="HTML",
            )
        else:
            await message.reply(
                f"⚠️ Ни один слайд не попал в диапазон. Проповедь не будет выделена.{warning}",
                parse_mode="HTML",
            )

        # Проверяем остаток
        remaining = [
            p for p in pending["prepared"]
            if "pngs_sorted" in p and p.get("start") is not None and not p.get("confirmed")
        ]

        if remaining:
            # ✅ Единая точка отрисовки — обновляет prompt_idx/prompt_nonce
            await _yd_render_sermon_prompt(
                task_id=tid,
                item=remaining[0],
                status_msg=None,
                reply_fn=message.reply,
            )
        else:
            # Всё подтверждено — загружаем.
            bot = pending.get("bot")
            if bot:
                class _FakeCallback:
                    def __init__(self, msg, user):
                        self.message = msg
                        self.from_user = user
                        self.data = ""
                        self.bot = bot

                fake_cb = _FakeCallback(message, message.from_user)
                await _yd_upload_files(
                    callback=fake_cb,
                    bot=bot,
                    task_id=tid,
                    status_msg=message,
                )

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
    """Проверяет владельца задачи по файлу .owner."""
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


# ==========================================
# 12. КОМАНДА /sunday — Яндекс.Диск
# ==========================================

@router.message(Command("sunday"))
async def cmd_sunday(message: types.Message, check_access):
    """Проверка Диска + вывод найденных pptx для ближайшего предстоящего воскресенья."""
    import html as html_module

    if not await check_access(message):
        return

    if yandex_client is None:
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

        # 1. Доступность
        ok, err = await yandex_client.check_access()
        if not ok:
            await status_msg.edit_text(
                f"❌ <b>Яндекс.Диск недоступен</b>\n\n"
                f"Причина: <code>{html_module.escape(str(err))}</code>\n\n"
                f"Проверьте токен в <code>config.ini</code>.",
                parse_mode="HTML",
            )
            return

        # 2. Дата
        sunday = get_nearest_sunday()
        sunday_str = sunday.strftime("%d.%m.%Y")
        month_str = month_folder_name(sunday)

        await status_msg.edit_text(
            f"✅ Яндекс.Диск доступен\n"
            f"📅 Ближайшее воскресенье: <b>{html_module.escape(sunday_str)}</b>\n"
            f"📁 Ожидаемая папка: <code>{html_module.escape(f'{month_str}/{sunday_str}')}</code>\n\n"
            f"🔍 Проверяю структуру папок...",
            parse_mode="HTML",
        )

        # 3. Разрешение путей
        paths = await resolve_sunday_paths(
            yandex_client, yandex_base_path, sunday,
            yandex_source_folder, yandex_target_folder,
        )
        if not paths:
            src_esc = html_module.escape(yandex_source_folder)
            tgt_esc = html_module.escape(yandex_target_folder)
            await status_msg.edit_text(
                f"❌ <b>Структура папок не найдена</b>\n\n"
                f"Ожидалось:\n"
                f"<code>{html_module.escape(yandex_base_path)}/</code>\n"
                f"<code>  {html_module.escape(month_str)}/</code>\n"
                f"<code>    {html_module.escape(sunday_str)}/</code>\n"
                f"<code>      {src_esc}/</code>\n"
                f"<code>      {tgt_esc}/</code>",
                parse_mode="HTML",
            )
            return

        # 4. Поиск pptx
        try:
            pptx_files = await find_pptx_in_source(
                yandex_client, paths["source"], sunday
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
            src_esc = html_module.escape(yandex_source_folder)
            await status_msg.edit_text(
                f"📅 Ближайшее воскресенье: <b>{html_module.escape(sunday_str)}</b>\n"
                f"📍 Папка: <code>{html_module.escape(paths['source'])}</code>\n\n"
                f"❌ <b>pptx-файлы не найдены.</b>\n\n"
                f"Положите pptx с датой <code>{sunday:%d.%m.%y}</code> "
                f"в папку <code>{src_esc}</code> и попробуйте снова.",
                parse_mode="HTML",
            )
            return

        # 5. Список с экранированием и лимитом
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

        # ✅ Проверяем, что нас не отменили за время работы
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

        # ✅ Единственная запись сессии — с nonce
        session_key = f"yd_{message.from_user.id}_{message.chat.id}"
        sessions[session_key] = {
            "user_id": message.from_user.id,
            "chat_id": message.chat.id,
            "sunday": sunday,
            "sunday_str": sunday_str,
            "paths": paths,
            "files": pptx_files,
            "nonce": nonce,
            "created_at": time.time(),
        }

        kb = InlineKeyboardBuilder()
        for idx, f in enumerate(pptx_files):
            prefix = "🎯" if "служение" in f["name"].lower() else "📄"
            kb.row(InlineKeyboardButton(
                text=f"{prefix} {f['name']}",
                callback_data=f"yd_pick:{message.from_user.id}:{nonce}:{idx}"
            ))
        if len(pptx_files) > 1:
            kb.row(InlineKeyboardButton(
                text="📁 Все подряд",
                callback_data=f"yd_pick:{message.from_user.id}:{nonce}:all"
            ))
        kb.row(InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=f"yd_cancel:{message.from_user.id}:{nonce}"
        ))

        await status_msg.edit_text(
            "\n".join(header_lines + body_lines),
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        session_created = True

        # ✅ Снимаем блокировку сразу после успешного показа
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
                    parse_mode="HTML"
                )
            else:
                await message.reply(f"❌ Ошибка: {str(e)[:200]}")
        except Exception:
            pass
    finally:
        if not session_created:
            # Освобождаем ТОЛЬКО наше поколение — чужие не трогаем
            released = await yd_release(message.from_user.id, message.chat.id, nonce)
            if released:
                logging.info(
                    f"🔓 Сессия {message.from_user.id}:{message.chat.id} "
                    f"освобождена (неудачный запуск, nonce={nonce})"
                )
            else:
                logging.info(
                    f"ℹ️ Сессия {message.from_user.id}:{message.chat.id} "
                    f"уже освобождена другим вызовом (nonce={nonce})"
                )


# ==========================================
# 13. yd_pick — выбор файла
# ==========================================

@router.callback_query(F.data.startswith("yd_pick:"))
async def yd_pick(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr):
    """Обработка выбранного файла: скачивание → конвертация → раскладка → загрузка."""
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

    # ✅ Атомарная проверка + пометка сессии как обрабатываемой
    async with yd_session_lock:
        session = sessions.get(session_key)
        if not session or session.get("nonce") != callback_nonce:
            await callback.answer("❌ Сессия неактивна.", show_alert=True)
            return

        # ✅ Помечаем сессию как "обрабатываемую"
        session["processing"] = True
        # Извлекаем данные сессии ДО её удаления
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

    # Отвечаем после успешной валидации
    if file_selector == "all":
        await callback.answer("⏳ Обрабатываю все файлы...")
    else:
        await callback.answer("⏳ Начинаю обработку...")

    # ✅ Вызываем пайплайн с извлечёнными данными
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


# ==========================================
# 14. yd_cancel — кнопка отмены
# ==========================================

@router.callback_query(F.data.startswith("yd_cancel:"))
async def yd_cancel_callback(callback: types.CallbackQuery):
    """Отмена сессии — только её владельцем и только для активной сессии."""
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
            "❌ Только автор запроса может отменить операцию.",
            show_alert=True
        )
        return

    session_key = f"yd_{owner_user_id}_{callback.message.chat.id}"
    session = sessions.get(session_key)

    # Проверка nonce — кнопка принадлежит текущей сессии?
    if session is None or session.get("nonce") != callback_nonce:
        await yd_release(owner_user_id, callback.message.chat.id, callback_nonce)
        try:
            await callback.message.edit_text("❌ Сессия уже неактивна.")
        except Exception:
            pass
        await callback.answer("❌ Сессия уже неактивна.", show_alert=True)
        return

    # ✅ Снимаем блокировку (если ещё висит) и удаляем сессию
    await yd_release(owner_user_id, callback.message.chat.id, callback_nonce)
    sessions.pop(session_key, None)

    try:
        await callback.message.edit_text("❌ Операция отменена.")
    except Exception:
        pass
    await callback.answer()


# ==========================================
# 15. /cancel_yd — команда отмены
# ==========================================

@router.message(Command("cancel_yd"))
async def cmd_cancel_yd(message: types.Message, check_access):
    if not await check_access(message):
        return

    session_key = f"yd_{message.from_user.id}_{message.chat.id}"
    session = sessions.get(session_key)

    # Освобождаем блокировку (если активна)
    released = await yd_release(message.from_user.id, message.chat.id)

    if session is None and released:
        await message.reply("ℹ️ У вас нет активной сессии Яндекс.Диска.")
        return

    # Удаляем сессию (если была)
    sessions.pop(session_key, None)

    await message.reply("✅ Сессия Яндекс.Диска сброшена.")


# ==========================================
# 16. yd_sermon_ok / skip / edit
# ==========================================

@router.callback_query(F.data.startswith("yd_sermon_ok:"))
async def yd_sermon_ok(callback: types.CallbackQuery, bot: Bot):
    """Пользователь подтвердил диапазон проповеди."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return
    task_id = parts[1]

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await callback.answer("❌ Сессия неактивна.", show_alert=True)
        return
    pending = session["pending"]

    # ✅ Атомарная проверка + потребление промпта
    claimed = await _yd_claim_prompt(callback)
    if claimed is None:
        return
    pending = claimed

    idx = int(parts[2])
    pending["prepared"][idx]["confirmed"] = True

    await callback.answer("✅ Диапазон подтверждён")

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
    """Пользователь пропустил определение проповеди — все PNG в общую папку."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return
    task_id = parts[1]

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await callback.answer("❌ Сессия неактивна.", show_alert=True)
        return
    pending = session["pending"]

    # ✅ Атомарная проверка + потребление промпта
    claimed = await _yd_claim_prompt(callback)
    if claimed is None:
        return
    pending = claimed

    idx = int(parts[2])
    pending["prepared"][idx]["start"] = None
    pending["prepared"][idx]["end"] = None
    pending["prepared"][idx]["ranges"] = None
    pending["prepared"][idx]["confirmed"] = True

    await callback.answer("⏭ Пропущено")

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
    """Пользователь хочет ввести диапазон вручную."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("❌ Некорректный запрос.", show_alert=True)
        return
    task_id = parts[1]

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        await callback.answer("❌ Сессия неактивна.", show_alert=True)
        return
    pending = session["pending"]

    # ✅ Атомарная проверка + потребление промпта
    claimed = await _yd_claim_prompt(callback)
    if claimed is None:
        return
    pending = claimed

    idx = int(parts[2])
    pending["awaiting_range_for_idx"] = idx

    current = pending["prepared"][idx]
    total_slides = len(current["pngs_sorted"])
    file_name_esc = html_module.escape(current["file_name"])

    await callback.answer()

    await callback.message.edit_text(
        f"✏️ <b>Введите диапазон проповеди</b>\n\n"
        f"📄 Файл: <code>{file_name_esc}</code>\n"
        f"📊 Всего слайдов: {total_slides}\n\n"
        f"Пример: <code>5-30</code> или <code>5,7,10-15</code>\n"
        f"Отправьте текстом в чат.",
        parse_mode="HTML",
    )


# ==========================================
# 17. ХЕЛПЕРЫ ПРОМПТОВ И ОЧИСТКИ
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
    pending = session["pending"]

    # Проверка, что промпт ещё актуален
    if pending.get("prompt_nonce") != nonce or pending.get("prompt_idx") != idx:
        await callback.answer("⏳ Промпт уже обработан.", show_alert=True)
        return None

    if callback.from_user.id != pending["owner_user_id"]:
        await callback.answer("❌ Только автор.", show_alert=True)
        return None

    # ✅ Помечаем промпт потреблённым — до мутации состояния
    pending["prompt_nonce"] = None
    pending["prompt_idx"] = None

    # Отключаем клавиатуру
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
    Всегда обновляет pending["prompt_idx"] и pending["prompt_nonce"].
    reply_fn — если нужно отправить новое сообщение (message.reply),
              иначе редактируем status_msg.
    """
    session = sessions.get(task_id)
    if not session or "pending" not in session:
        return
    pending = session["pending"]

    # ✅ Всегда обновляем idx под текущий item
    pending["current_confirm_idx"] = pending["prepared"].index(item)
    pending["prompt_idx"] = pending["prepared"].index(item)

    # ✅ Генерируем новый nonce для каждого нового промпта
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
            callback_data=f"yd_sermon_ok:{task_id}:{pending['prompt_idx']}:{prompt_nonce}"
        ),
        InlineKeyboardButton(
            text="✏️ Изменить",
            callback_data=f"yd_sermon_edit:{task_id}:{pending['prompt_idx']}:{prompt_nonce}"
        ),
        InlineKeyboardButton(
            text="⏭ Пропустить",
            callback_data=f"yd_sermon_skip:{task_id}:{pending['prompt_idx']}:{prompt_nonce}"
        ),
    )

    if reply_fn is not None:
        await reply_fn(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


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
    Идемпотентная очистка. Безопасно вызывать многократно.
    """
    # 1. Папка задачи
    try:
        if task_dir and task_dir.exists():
            shutil.rmtree(task_dir)
            logging.info(f"🧹 Удалена папка задачи: {task_dir}")
    except Exception as e:
        logging.error(f"Ошибка удаления task_dir {task_dir}: {e}")

    # 2. Сессии (nonce-safe для picker-сессии)
    async with yd_session_lock:
        current = sessions.get(session_key)
        if current is not None and current.get("nonce") == nonce:
            sessions.pop(session_key, None)
        sessions.pop(task_id, None)

    # 3. Блокировка Яндекс.Диска
    await yd_release(owner_user_id, chat_id, nonce)

    # 4. Сообщение об ошибке
    if error is not None and bot is not None and status_msg is not None:
        try:
            await status_msg.edit_text(
                f"❌ <b>Ошибка обработки</b>\n\n"
                f"<code>{html_module.escape(str(error)[:200])}</code>\n\n"
                f"Временные файлы удалены. Попробуйте снова.",
                parse_mode="HTML",
            )
        except Exception:
            pass


def _is_sermon_slide(item: dict, slide_idx: int) -> bool:
    """
    Проверяет, принадлежит ли слайд (1-based) одному из диапазонов проповеди.
    Приоритет — item["ranges"] (список диапазонов).
    Fallback — item["start"]..item["end"] (старое поведение).
    """
    ranges = item.get("ranges")
    if ranges:
        return any(s <= slide_idx <= e for s, e in ranges)
    start = item.get("start")
    end = item.get("end")
    return start is not None and start <= slide_idx <= end


# ==========================================
# ПАЙПЛАЙН ОБРАБОТКИ ФАЙЛОВ ЯНДЕКС.ДИСКА
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
    Первая фаза: скачивание + конвертация всех файлов.
    Сохраняет результат в sessions[task_id]["pending"].
    Затем либо сразу загружает (если проповедь не найдена),
    либо спрашивает подтверждение через кнопки.
    """
    import html as html_module

    status_msg = callback.message
    owner_user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    task_id = f"yd_task_{owner_user_id}_{secrets.token_hex(4)}"
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Загружаем шаблон структуры
        template_path = Path(__file__).parent / yandex_template_file
        structure = load_template(template_path)
        if structure is None:
            await status_msg.edit_text(
                f"❌ <b>Ошибка загрузки template.yaml</b>\n\n"
                f"Файл: <code>{html_module.escape(str(template_path))}</code>",
                parse_mode="HTML"
            )
            await _yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
                bot=None, status_msg=None, error=None,
            )
            return

        # Создаём структуру в папке даты
        await status_msg.edit_text("📁 Создаю структуру папок...")
        structure_base = paths["date_folder"]
        ok = await create_structure(yandex_client, structure_base, structure)
        if not ok:
            await status_msg.edit_text(
                "❌ <b>Не удалось создать структуру папок</b>\n\n"
                "Проверьте права на Яндекс.Диске.",
                parse_mode="HTML"
            )
            await _yd_cleanup_task(
                task_id, session_key, task_dir,
                owner_user_id, chat_id, nonce,
                bot=None, status_msg=None, error=None,
            )
            return

        target_base = paths["target"]

        # Готовим список подготовленных файлов
        prepared = []

        for f_idx, pptx_item in enumerate(files_to_process, start=1):
            file_name = pptx_item["name"]
            file_name_esc = html_module.escape(file_name)

            await status_msg.edit_text(
                f"📥 Скачиваю <code>{file_name_esc}</code>...",
                parse_mode="HTML"
            )

            local_pptx = task_dir / file_name
            ok = await yandex_client.download_file(pptx_item["path"], local_pptx)
            if not ok:
                prepared.append({
                    "file_name": file_name,
                    "file_slug": safe_folder_name(file_name),
                    "failed_at_stage": "download",
                })
                continue

            await status_msg.edit_text(
                f"⚙️ Конвертирую <code>{file_name_esc}</code> в PNG...",
                parse_mode="HTML"
            )
            temp_png_dir = task_dir / f"png_{f_idx}"
            temp_png_dir.mkdir(exist_ok=True)

            try:
                pngs, used_pptx = await convert_all_pngs(
                    local_pptx, temp_png_dir,
                    user_mgr.get_user_config(owner_user_id)["quality"]
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

            # Извлекаем заметки
            notes_ok, notes, incomplete = await asyncio.to_thread(
                extract_speaker_notes, str(used_pptx)
            )
            if not notes_ok:
                notes = {}
            # Удаляем временный .pptx
            if used_pptx != local_pptx and used_pptx.exists():
                try:
                    used_pptx.unlink()
                except Exception:
                    pass

            # Определяем проповедь
            if incomplete:
                start, end, matches = None, None, []
                incomplete_warning = (
                    "⚠️ Заметки прочитаны частично, "
                    "проповедь не определена автоматически."
                )
            else:
                start, end, matches = find_sermon_range(notes, yandex_sermon_keyword)
                if matches and len(matches) == 1:
                    start, end = None, None
                incomplete_warning = None

            # ✅ Сохраняем ranges для консистентности
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

        # Сохраняем состояние в sessions
        pending = {
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
        }
        sessions[task_id] = {
            "user_id": owner_user_id,
            "chat_id": chat_id,
            "pending": pending,
            "nonce": nonce,
            "created_at": time.time(),
        }

        # Есть ли файлы, требующие подтверждения?
        needs_confirm = [
            p for p in prepared
            if "pngs_sorted" in p and p.get("start") is not None and not p.get("confirmed")
        ]

        if not needs_confirm:
            # Нет проповедей — сразу загружаем
            await _yd_upload_files(
                callback=callback,
                bot=bot,
                task_id=task_id,
                status_msg=status_msg,
            )
            return

        # Есть файлы, требующие подтверждения — показываем кнопки
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
        return


async def _yd_ask_sermon_confirmation(
    callback: types.CallbackQuery,
    task_id: str,
    status_msg,
    needs_confirm: list,
):
    """Показывает пользователю найденные слайды проповеди и кнопки действий."""
    if not needs_confirm:
        return
    await _yd_render_sermon_prompt(task_id, needs_confirm[0], status_msg)


async def _yd_upload_files(
    callback: types.CallbackQuery,
    bot: Bot,
    task_id: str,
    status_msg,
):
    """Загружает PNG всех подготовленных файлов + формирует отчёт."""
    import html as html_module

    session = sessions.get(task_id)
    if not session or "pending" not in session:
        return
    pending = session["pending"]
    prepared = pending["prepared"]
    target_base = pending["target_base"]
    task_dir = pending["task_dir"]
    owner_user_id = pending["owner_user_id"]
    chat_id = pending["chat_id"]
    session_key = pending["session_key"]
    nonce = pending["nonce"]

    try:
        total_uploaded = 0
        total_failed = 0
        report_lines = [f"📁 Обработано файлов: <b>{len(prepared)}</b>\n"]

        for f_idx, item in enumerate(prepared, start=1):
            file_name = item["file_name"]
            file_name_esc = html_module.escape(file_name)

            # Ошибки на этапе подготовки
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

            await status_msg.edit_text(
                f"📤 Загружаю PNG на Яндекс.Диск ({file_name_esc})...",
                parse_mode="HTML"
            )

            pptx2png_dir = f"{target_base}/{yandex_pptx2png_folder}/{file_slug}"
            sermon_dir = f"{target_base}/{yandex_sermon_folder}"

            ok1 = await yandex_client.ensure_folder(pptx2png_dir)
            ok2 = await yandex_client.ensure_folder(sermon_dir)
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
                is_sermon = _is_sermon_slide(item, slide_idx)
                if is_sermon:
                    remote_path = f"{sermon_dir}/{file_slug}_{png_path.name}"
                else:
                    remote_path = f"{pptx2png_dir}/{png_path.name}"

                ok = await yandex_client.upload_file(png_path, remote_path)
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

        # Итоговый отчёт
        if total_failed > 0:
            report_lines.append(
                f"\n⚠️ Всего загружено PNG: <b>{total_uploaded}</b>\n"
                f"❌ Ошибок: <b>{total_failed}</b>"
            )
        else:
            report_lines.append(f"\n📊 Всего загружено PNG: <b>{total_uploaded}</b>")

        report_lines.append(
            f"\n🔗 <a href=\"https://disk.yandex.ru/client/disk"
            f"{html_module.escape(pending.get('target_base', ''))}\">Открыть на Яндекс.Диске</a>"
        )

        # Разбивка и отправка отчёта
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
        await _yd_cleanup_task(
            task_id, session_key, task_dir,
            owner_user_id, chat_id, nonce,
            bot=bot, status_msg=status_msg, error=e,
        )
        return
    finally:
        # ✅ Идемпотентная очистка на любом пути
        await _yd_cleanup_task(
            task_id, session_key, task_dir,
            owner_user_id, chat_id, nonce,
            bot=None, status_msg=None, error=None,
        )


async def _yd_send_report(
    bot: Bot,
    chat_id: int,
    status_msg,
    report_lines: list,
    total_uploaded: int,
    total_failed: int,
):
    """Разбивает отчёт на chunks и отправляет с обработкой ошибок."""
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
                chunks[0],
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            delivered += 1
            first_delivered = True
        except Exception as e:
            logging.error(f"Ошибка edit_text первой части: {e}", exc_info=True)
            try:
                await bot.send_message(
                    chat_id=chat_id, text=chunks[0],
                    parse_mode="HTML", disable_web_page_preview=True,
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
        except Exception:
            pass

    for i, chunk in enumerate(chunks[1:], start=2):
        try:
            await bot.send_message(
                chat_id=chat_id, text=chunk,
                parse_mode="HTML", disable_web_page_preview=True,
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
        except Exception:
            pass