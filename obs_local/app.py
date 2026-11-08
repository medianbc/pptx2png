"""Локальный пайплайн: Яндекс.Диск -> заметки -> PNG для OBS."""

from __future__ import annotations

import asyncio
import configparser
from contextlib import contextmanager
import errno
import getpass
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from pptx import Presentation

from pptx2png_core.converter_engine import convert_all_pngs, ppt_to_pptx_crossplatform
from pptx2png_core.sermon_detector import find_category_range
from pptx2png_core.utils import extract_speaker_notes
from pptx2png_core.yandex_disk import (
    YandexDiskClient,
    find_pptx_in_source,
    get_nearest_sunday,
    resolve_sunday_paths,
)

from .config_utils import update_ini_file as _update_ini_file
from .config_utils import get_settings_path, parse_local_file_path


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
CATEGORIES = {
    "sermon": "Проповедь",
    "opening": "Начало собрания",
    "prayer": "Молитва",
}
DEFAULT_ROOT_PATH = "./obs_png"


class OperationCancelled(Exception):
    """Raised when the operator cancels an interactive OBS preparation."""


def _read_yandex_token() -> str:
    for path in (BASE_DIR / "config.ini", PROJECT_DIR / "config.ini"):
        if not path.is_file():
            continue
        secrets = configparser.ConfigParser(interpolation=None)
        secrets.read(path, encoding="utf-8")
        token = secrets.get("YandexDisk", "token", fallback="").strip()
        if token:
            return token
    return ""


def load_config(
    *,
    interactive: bool = False,
    input_fn=None,
    secret_input_fn=None,
) -> dict[str, Any]:
    input_fn = input_fn or input
    secret_input_fn = secret_input_fn or getpass.getpass
    settings = configparser.ConfigParser(interpolation=None)
    settings_path = get_settings_path(BASE_DIR)

    if not settings.read(settings_path, encoding="utf-8"):
        raise RuntimeError(f"Не найден файл настроек: {settings_path}")

    def setting(section: str, option: str, fallback: str = "") -> str:
        if not settings.has_section(section):
            return fallback
        return settings.get(section, option, fallback=fallback).strip()

    base_path = setting("YandexDisk", "base_path")
    source_folder = setting("YandexDisk", "source_folder", "Служение")
    root_value = setting("OBS Local paths", "root_path")
    category_output_value = setting(
        "OBS Local paths", "category_output_path", "Трансляция"
    )
    quality = setting("OBS Local paths", "quality").lower()

    settings_updates: dict[str, dict[str, str]] = {}
    if not base_path and interactive:
        base_path = input_fn(
            "Путь к папке Яндекс.Диска с презентациями: "
        ).strip()
        if not base_path or "\n" in base_path:
            raise RuntimeError("Путь к папке Яндекс.Диска не задан")
        settings_updates.setdefault("YandexDisk", {})["base_path"] = base_path

    if not root_value and interactive:
        root_value = (
            input_fn(
                f"Локальный каталог для PNG [{DEFAULT_ROOT_PATH}]: "
            ).strip()
            or DEFAULT_ROOT_PATH
        )
        if "\n" in root_value:
            raise RuntimeError("Путь для PNG должен быть одной строкой")
        settings_updates.setdefault("OBS Local paths", {})["root_path"] = root_value

    if quality not in {"standard", "2k", "4k"} and interactive:
        while True:
            choice = input_fn(
                "Качество PNG: 1. standard, 2. 2k, 3. 4k [1]: "
            ).strip() or "1"
            quality = {"1": "standard", "2": "2k", "3": "4k"}.get(choice, "")
            if quality in {"standard", "2k", "4k"}:
                break
            print("Введите 1, 2 или 3.")
        settings_updates.setdefault("OBS Local paths", {})["quality"] = quality

    if settings_updates:
        _update_ini_file(settings_path, settings_updates)
        settings.read(settings_path, encoding="utf-8")

    token = _read_yandex_token()
    if not token and interactive:
        token = secret_input_fn("OAuth-токен Яндекс.Диска: ").strip()
        if not token or "\n" in token:
            raise RuntimeError("OAuth-токен Яндекс.Диска не задан")
        _update_ini_file(
            BASE_DIR / "config.ini",
            {"YandexDisk": {"token": token}},
            file_mode=0o600,
        )

    if not token:
        raise RuntimeError("В config.ini не задан [YandexDisk] token")
    if not base_path:
        raise RuntimeError(f"В {settings_path.name} не задан [YandexDisk] base_path")
    if not root_value:
        raise RuntimeError(f"В {settings_path.name} не задан [OBS Local paths] root_path")
    if quality not in {"standard", "2k", "4k"}:
        raise RuntimeError("quality должен быть standard, 2k или 4k")

    root_path = Path(root_value).expanduser()
    if not root_path.is_absolute():
        root_path = PROJECT_DIR / root_path

    category_output_path = Path(category_output_value).expanduser()
    if not category_output_path.is_absolute():
        category_output_path = root_path / category_output_path

    legacy_keyword = setting("YandexDisk", "sermon_keyword", "проповед")
    raw_keywords = {
        "sermon": setting(
            "SlideCategories", "sermon_keywords", legacy_keyword
        ),
        "opening": setting(
            "SlideCategories", "opening_keywords", "начало,в начале"
        ),
        "prayer": setting(
            "SlideCategories", "prayer_keywords", "молитва,молиться"
        ),
    }
    keywords = {
        category: [word.strip().lower() for word in raw.split(",") if word.strip()]
        for category, raw in raw_keywords.items()
    }

    return {
        "token": token,
        "base_path": base_path,
        "source_folder": source_folder,
        "root_path": root_path,
        "category_output_path": category_output_path,
        "quality": quality,
        "keywords": keywords,
    }


async def download_presentation(
    config: dict[str, Any],
    sunday: datetime,
    temp_dir: Path,
    input_fn: Callable[[str], str] | None = None,
) -> Path:
    if input_fn is None:
        input_fn = input

    async with aiohttp.ClientSession() as http_session:
        client = YandexDiskClient(config["token"], http_session)
        accessible, error = await client.check_access()
        if not accessible:
            raise RuntimeError(f"Нет доступа к Яндекс.Диску: {error}")

        while True:
            paths = await resolve_sunday_paths(
                client,
                config["base_path"],
                sunday,
                source_folder=config["source_folder"],
            )
            files = (
                await find_pptx_in_source(client, paths["source"], sunday)
                if paths
                else []
            )
            if files:
                break

            if paths:
                problem = (
                    f"В папке служения не найдены PPT/PPTX "
                    f"за {sunday:%d.%m.%Y}."
                )
            else:
                problem = f"Папка служения за {sunday:%d.%m.%Y} не найдена."
            print(f"\n{problem}")
            print("Можно продолжить подготовку одним из способов:")
            print("  1. Повторить поиск на Яндекс.Диске")
            print("  2. Открыть локальный файл (.pptx или .ppt)")
            print("  0. Вернуться в меню")
            try:
                answer = input_fn("Ваш выбор [0]: ").strip() or "0"
            except EOFError as error:
                raise OperationCancelled from error

            if answer == "0":
                raise OperationCancelled
            if answer == "1":
                continue
            if answer != "2":
                print("Введите 1, 2 или 0.")
                continue
            try:
                answer = input_fn("Путь к файлу PPT/PPTX: ").strip()
            except EOFError as error:
                raise OperationCancelled from error

            local_path = parse_local_file_path(answer)
            if local_path.suffix.lower() not in {".ppt", ".pptx"}:
                print("Укажите путь к файлу .ppt или .pptx.")
                continue
            if not local_path.is_file():
                print(f"Файл не найден: {local_path}")
                continue
            return local_path

        print(f"\nПрезентации на {sunday:%d.%m.%Y}:")
        for index, item in enumerate(files, start=1):
            print(f"  {index}. {item['name']}")

        while True:
            try:
                answer = input_fn("Номер презентации [0 — отмена]: ").strip() or "0"
                if answer == "0":
                    raise OperationCancelled
                choice = int(answer) - 1
            except EOFError as error:
                raise OperationCancelled from error
            except ValueError:
                choice = -1
            if 0 <= choice < len(files):
                break
            print("Введите номер из списка.")

        selected = files[choice]
        file_name = Path(selected["name"]).name
        destination = temp_dir / file_name
        remote_path = selected.get("path") or f"{paths['source']}/{file_name}"
        if not await client.download_file(remote_path, destination):
            raise RuntimeError(f"Не удалось скачать {file_name}")
        return destination


def _normalize_presentation(source_path: Path, temp_dir: Path) -> Path:
    if source_path.suffix.lower() == ".ppt":
        return ppt_to_pptx_crossplatform(source_path, temp_dir)
    return source_path


def _find_categories(
    notes: dict[int, str], keywords: dict[str, list[str]]
) -> dict[str, tuple[int | None, int | None, list[int]]]:
    results = {}
    print("\nСовпадения в заметках:")
    for category, label in CATEGORIES.items():
        start, end, matches = find_category_range(notes, keywords[category])
        results[category] = (start, end, matches)
        if matches:
            print(
                f"  {label}: слайды {', '.join(map(str, matches))}; "
                f"предлагаемый диапазон {start}-{end}"
            )
        else:
            print(f"  {label}: совпадений нет")
    return results


def _select_range(
    results: dict[str, tuple[int | None, int | None, list[int]]],
    total_slides: int,
    category_ranges: dict[str, list[tuple[int, int]]] | None = None,
) -> tuple[str, list[tuple[int, int]]]:
    if category_ranges is None:
        category_ranges = {}
    while True:
        print("\nВыберите способ обработки:")
        print("  1. Все слайды")
        print("  2. Категория по заметкам / изменить диапазон категории")
        print("  3. Произвольные номера и диапазоны")
        print("  4. Подготовить несколько категорий")
        print("  0. Отменить")
        try:
            choice = input("Режим [1]: ").strip() or "1"
        except EOFError as error:
            raise OperationCancelled from error
        aliases = {
            "1": "all",
            "2": "category",
            "3": "custom",
            "4": "categories",
            "0": "cancel",
        }
        mode = aliases.get(choice)
        if mode not in {"all", "category", "custom", "categories", "cancel"}:
            print("Введите 1, 2, 3, 4 или 0.")
            continue
        if mode == "cancel":
            raise OperationCancelled
        if mode == "all":
            return "all", [(1, total_slides)]
        if mode == "categories":
            return "categories", []

        category = "custom"
        default = ""
        if mode == "category":
            print("\nКатегории:")
            categories = list(CATEGORIES)
            for index, key in enumerate(categories, start=1):
                start, end, _ = results[key]
                suggested = f"{start}-{end}" if start is not None else "не найден"
                if key in category_ranges:
                    suggested = _format_ranges(category_ranges[key])
                print(f"  {index}. {CATEGORIES[key]} ({suggested})")
            while True:
                try:
                    answer = input("Номер категории [0 — назад]: ").strip() or "0"
                except EOFError as error:
                    raise OperationCancelled from error
                if answer == "0":
                    break
                try:
                    category_index = int(answer) - 1
                except ValueError:
                    category_index = -1
                if 0 <= category_index < len(categories):
                    category = categories[category_index]
                    break
                print("Введите номер категории из списка.")
            if answer == "0":
                continue

            start, end, _ = results[category]
            if start is not None and end is not None:
                default = f"{start}-{end}"
            if category in category_ranges:
                default = _format_ranges(category_ranges[category])
            if not default:
                print("Диапазон по заметкам не найден; задайте его вручную.")

        ranges = _prompt_slide_ranges(default, total_slides)
        if ranges is not None:
            if category in CATEGORIES:
                category_ranges[category] = ranges
            return category, ranges


def _format_ranges(ranges: list[tuple[int, int]]) -> str:
    return ", ".join(
        str(first) if first == last else f"{first}-{last}"
        for first, last in ranges
    )


def _prompt_slide_ranges(default: str, total_slides: int) -> list[tuple[int, int]] | None:
    while True:
        try:
            value = input(
                f"Слайды [{default or 'обязательно'}; формат 1, 3-5; 0 — назад]: "
            ).strip() or default
        except EOFError as error:
            raise OperationCancelled from error
        if value == "0":
            return None
        try:
            return _parse_slide_ranges(value, total_slides)
        except ValueError as error:
            print(f"{error} Например: 1, 3-5, 8.")


def _select_categories(
    category_ranges: dict[str, list[tuple[int, int]]],
    selected: set[str],
    total_slides: int,
) -> dict[str, list[tuple[int, int]]] | None:
    categories = list(CATEGORIES)
    while True:
        print("\nПодготовка категорий (диапазоны сохраняются до завершения этой презентации):")
        for index, key in enumerate(categories, 1):
            label = _format_ranges(category_ranges[key]) if key in category_ranges else "не задан"
            mark = "выбрана" if key in selected else "не выбрана"
            print(f"  {index}. {CATEGORIES[key]} ({label}; {mark}) — изменить диапазон")
        print("  4. Конвертировать выбранные категории")
        print("  5. Выбрать / исключить категорию")
        print("  0. Назад")
        try:
            answer = input("Действие [4]: ").strip() or "4"
        except EOFError as error:
            raise OperationCancelled from error
        if answer == "0":
            return None
        if answer == "4":
            if not selected:
                print("Выберите хотя бы одну категорию с заданным диапазоном.")
                continue
            return {key: category_ranges[key] for key in categories if key in selected}
        if answer == "5":
            try:
                index = input("Номер категории [0 — назад]: ").strip() or "0"
            except EOFError as error:
                raise OperationCancelled from error
            if index == "0":
                continue
            if not index.isdecimal() or not 1 <= int(index) <= len(categories):
                print("Введите номер категории из списка.")
                continue
            key = categories[int(index) - 1]
            if key in selected:
                selected.remove(key)
            elif key in category_ranges:
                selected.add(key)
            else:
                print("Сначала задайте диапазон этой категории.")
            continue
        if not answer.isdecimal() or not 1 <= int(answer) <= len(categories):
            print("Введите 1, 2, 3, 4, 5 или 0.")
            continue
        key = categories[int(answer) - 1]
        ranges = _prompt_slide_ranges(
            _format_ranges(category_ranges.get(key, [])), total_slides,
        )
        if ranges is not None:
            category_ranges[key] = ranges
            selected.add(key)


def _parse_slide_ranges(value: str, total_slides: int) -> list[tuple[int, int]]:
    if not value.strip():
        raise ValueError("Введите хотя бы один слайд или диапазон.")

    ranges = []
    for part in value.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:[-–]\s*(\d+)\s*)?", part)
        if not match:
            raise ValueError(f"Некорректный диапазон: {part.strip()!r}.")
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first or last > total_slides:
            raise ValueError(
                f"Номера слайдов должны быть от 1 до {total_slides}; "
                f"получено {part.strip()!r}."
            )
        ranges.append((first, last))

    merged = []
    for first, last in sorted(set(ranges)):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def _confirm_selection(
    category: str,
    ranges: list[tuple[int, int]],
    total_slides: int,
    input_fn=None,
) -> bool:
    selected = _format_ranges(ranges)
    count = sum(last - first + 1 for first, last in ranges)
    print(f"\nВыбрано: {selected} ({count} из {total_slides} слайдов)")
    print(f"Каталог категории: {CATEGORIES.get(category, category)}")
    return _confirm_action(input_fn)


def _confirm_action(input_fn=None) -> bool:
    input_fn = input_fn or input
    while True:
        try:
            answer = input_fn(
                "1. Конвертировать выбранные\n2. Изменить выбор\n0. Отмена\nВаш выбор [1]: "
            ).strip() or "1"
        except EOFError as error:
            raise OperationCancelled from error
        if answer == "1":
            return True
        if answer == "2":
            return False
        if answer == "0":
            raise OperationCancelled
        print("Введите 1, 2 или 0.")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    return cleaned or "presentation"


@contextmanager
def _category_output_lock(output_root: Path):
    lock_path = output_root / ".category-output.lock"
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in {
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLOCK,
                    }:
                        raise
                    time.sleep(0.1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _remove_category_path(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _cleanup_category_backups(
    backup_root: Path, keep_backup: Path | None = None
) -> None:
    try:
        if not backup_root.is_dir():
            return
        generated_backups = sorted(
            path
            for path in backup_root.iterdir()
            if path.is_dir() and not path.is_symlink()
            and re.fullmatch(r"\d{8}-\d{6}-\d{6}-[0-9a-f]{8}", path.name)
        )
    except OSError as error:
        print(
            "Предупреждение: не удалось проверить старые резервные копии "
            f"{backup_root}: {error}"
        )
        return
    if keep_backup is None and generated_backups:
        keep_backup = generated_backups[-1]
    for old_backup in generated_backups:
        if old_backup == keep_backup:
            continue
        try:
            shutil.rmtree(old_backup)
        except OSError as error:
            print(
                "Предупреждение: не удалось удалить старую резервную копию "
                f"{old_backup}: {error}"
            )


def _publish_category_outputs(
    staged_outputs: dict[str, Path], output_root: Path
) -> dict[str, Path]:
    published: list[dict[str, Any]] = []
    backups: dict[str, Path] = {}
    with _category_output_lock(output_root):
        try:
            for key, staging in staged_outputs.items():
                target = output_root / CATEGORIES[key]
                backup = None
                if target.exists() or target.is_symlink():
                    backup_root = output_root / ".backups" / target.name
                    backup_root.mkdir(parents=True, exist_ok=True)
                    backup = backup_root / (
                        f"{datetime.now():%Y%m%d-%H%M%S-%f}-"
                        f"{uuid.uuid4().hex[:8]}"
                    )
                    target.rename(backup)
                    backups[key] = backup
                state = {
                    "target": target,
                    "backup": backup,
                    "published": False,
                }
                published.append(state)
                staging.rename(target)
                state["published"] = True
        except BaseException as error:
            rollback_errors = []
            for state in reversed(published):
                target = state["target"]
                backup = state["backup"]
                try:
                    if state["published"] and (
                        target.exists() or target.is_symlink()
                    ):
                        _remove_category_path(target)
                    if backup is not None and (
                        backup.exists() or backup.is_symlink()
                    ):
                        backup.rename(target)
                except OSError as rollback_error:
                    rollback_errors.append(f"{target}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    "Не удалось полностью отменить публикацию категорий. "
                    "Резервные копии сохранены; проверьте папку .backups. "
                    + "; ".join(rollback_errors)
                ) from error
            if not isinstance(error, Exception):
                raise
            raise RuntimeError(
                f"Не удалось опубликовать категории; предыдущие файлы восстановлены: {error}"
            ) from error

        for key in staged_outputs:
            _cleanup_category_backups(
                output_root / ".backups" / CATEGORIES[key], backups.get(key)
            )
    return backups


async def prepare_obs_folder(
    config: dict[str, Any], source_path: Path, sunday: datetime
) -> Path:
    with tempfile.TemporaryDirectory(prefix="pptx2png_render_") as temp_name:
        temp_dir = Path(temp_name)
        normalized = _normalize_presentation(source_path, temp_dir)
        notes_ok, notes, incomplete = extract_speaker_notes(str(normalized))
        if not notes_ok:
            print("Не удалось прочитать заметки; можно выбрать все слайды вручную.")
            notes = {}
        elif incomplete:
            print("Предупреждение: часть заметок прочитать не удалось.")

        results = _find_categories(notes, config["keywords"])
        total_slides = len(Presentation(str(normalized)).slides)
        category_ranges = {
            key: [(start, end)]
            for key, (start, end, _) in results.items()
            if start is not None and end is not None and 1 <= start <= end <= total_slides
        }
        selected_categories = set(category_ranges)
        while True:
            category, ranges = _select_range(results, total_slides, category_ranges)
            if category == "categories":
                while True:
                    selections = _select_categories(
                        category_ranges, selected_categories, total_slides,
                    )
                    if selections is None:
                        break
                    print("\nВыбранные категории:")
                    for key, selected_ranges in selections.items():
                        print(f"  {CATEGORIES[key]}: {_format_ranges(selected_ranges)}")
                    if _confirm_action():
                        break
                if selections is None:
                    continue
                break
            if category in CATEGORIES:
                selected_categories.add(category)
            if _confirm_selection(category, ranges, total_slides):
                selections = {category: ranges}
                break

        render_dir = temp_dir / "rendered"
        render_dir.mkdir()
        png_paths, _ = await convert_all_pngs(
            normalized, render_dir, config["quality"]
        )
        highest_slide = max(
            last for selected_ranges in selections.values() for _, last in selected_ranges
        )
        if highest_slide > len(png_paths):
            raise RuntimeError(
                f"В PDF {len(png_paths)} страниц, а выбран слайд {highest_slide}"
            )

        if all(key in CATEGORIES for key in selections):
            category_output_path = Path(config.get(
                "category_output_path", config["root_path"] / "Трансляция"
            ))
            category_output_path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".category-staging-", dir=category_output_path
            ) as staging_name:
                staging_root = Path(staging_name)
                staged_outputs = {}
                for key, selected_ranges in selections.items():
                    output_dir = staging_root / key
                    output_dir.mkdir()
                    for first, last in selected_ranges:
                        for png_path in png_paths[first - 1 : last]:
                            shutil.copy2(png_path, output_dir / png_path.name)
                    staged_outputs[key] = output_dir
                backups = _publish_category_outputs(
                    staged_outputs, category_output_path
                )
                for backup in backups.values():
                    print(f"Предыдущий результат сохранён: {backup}")
            return (
                category_output_path
                if category == "categories"
                else category_output_path / CATEGORIES[category]
            )

        run_id = datetime.now().strftime("%H%M%S_%f")
        run_dir = (
            config["root_path"]
            / sunday.strftime("%Y-%m-%d")
            / _safe_name(source_path.stem)
            / run_id
        )
        output_dir = run_dir / category
        output_dir.mkdir(parents=True, exist_ok=False)
        for first, last in selections[category]:
            for png_path in png_paths[first - 1 : last]:
                shutil.copy2(png_path, output_dir / png_path.name)
        return output_dir


async def run(date: datetime | None = None) -> Path:
    config = load_config(interactive=True)
    sunday = date or get_nearest_sunday()
    config["root_path"].mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pptx2png_source_") as temp_name:
        source = await download_presentation(config, sunday, Path(temp_name))
        output_dir = await prepare_obs_folder(config, source, sunday)

    print(f"\nГотово. Папка PNG для OBS: {output_dir}")
    return output_dir


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Скачать презентацию с Яндекс.Диска и подготовить PNG для OBS."
    )
    parser.add_argument(
        "--date",
        help="Дата в формате YYYY-MM-DD; по умолчанию используется ближайшее воскресенье.",
    )
    args = parser.parse_args()
    selected_date = None
    if args.date:
        try:
            selected_date = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError as error:
            parser.error(f"Неверный формат даты: {error}")

    try:
        asyncio.run(run(selected_date))
    except OperationCancelled:
        print("Подготовка отменена.")
    except Exception as error:
        parser.exit(1, f"Ошибка: {error}\n")
