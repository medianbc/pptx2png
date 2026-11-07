"""Локальный пайплайн: Яндекс.Диск -> заметки -> PNG для OBS."""

from __future__ import annotations

import asyncio
import configparser
import getpass
import re
import shutil
import tempfile
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


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
CATEGORIES = {"sermon": "Проповедь", "opening": "Начало", "prayer": "Молитва"}
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
    settings_path = BASE_DIR / "settings.ini"

    if not settings.read(settings_path, encoding="utf-8"):
        raise RuntimeError(f"Не найден файл настроек: {settings_path}")

    def setting(section: str, option: str, fallback: str = "") -> str:
        if not settings.has_section(section):
            return fallback
        return settings.get(section, option, fallback=fallback).strip()

    base_path = setting("YandexDisk", "base_path")
    source_folder = setting("YandexDisk", "source_folder", "Служение")
    root_value = setting("OBS Local paths", "root_path")
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
            quality = input_fn(
                "Качество PNG [standard/2k/4k, по умолчанию standard]: "
            ).strip().lower() or "standard"
            if quality in {"standard", "2k", "4k"}:
                break
            print("Введите standard, 2k или 4k.")
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
        raise RuntimeError("В settings.ini не задан [YandexDisk] base_path")
    if not root_value:
        raise RuntimeError("В settings.ini не задан [OBS Local paths] root_path")
    if quality not in {"standard", "2k", "4k"}:
        raise RuntimeError("quality должен быть standard, 2k или 4k")

    root_path = Path(root_value).expanduser()
    if not root_path.is_absolute():
        root_path = PROJECT_DIR / root_path

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
            print("  Enter       — повторить поиск на Яндекс.Диске")
            print("  путь к PPTX — открыть локальный файл (.pptx или .ppt)")
            print("  esc / exit  — введите для возврата в меню")
            try:
                answer = input_fn("Ваш выбор: ").strip()
            except EOFError as error:
                raise OperationCancelled from error

            if answer.lower() in {
                "\x1b",
                "esc",
                "exit",
                "quit",
                "q",
                "выход",
                "отмена",
            }:
                raise OperationCancelled
            if not answer:
                continue

            local_path = Path(answer.strip("\"'")).expanduser()
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
                answer = input_fn("Номер презентации [0 — отмена]: ").strip()
                if answer.lower() in {"0", "q", "cancel", "отмена"}:
                    raise OperationCancelled
                choice = int(answer) - 1
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
    results: dict[str, tuple[int | None, int | None, list[int]]], total_slides: int
) -> tuple[str, list[tuple[int, int]]]:
    while True:
        print("\nВыберите способ обработки:")
        print("  all      — все слайды")
        print("  category — категория по заметкам или заданный вручную диапазон")
        print("  custom   — произвольные номера и диапазоны")
        print("  cancel   — отменить")
        mode = input("Режим [all]: ").strip().lower() or "all"
        aliases = {
            "1": "all",
            "2": "category",
            "3": "custom",
            "q": "cancel",
            "отмена": "cancel",
        }
        mode = aliases.get(mode, mode)
        if mode not in {"all", "category", "custom", "cancel"}:
            print("Введите all, category, custom или cancel.")
            continue
        if mode == "cancel":
            raise OperationCancelled
        if mode == "all":
            return "all", [(1, total_slides)]

        category = "custom"
        default = ""
        if mode == "category":
            print("\nКатегории:")
            categories = list(CATEGORIES)
            for index, key in enumerate(categories, start=1):
                start, end, _ = results[key]
                suggested = f"{start}-{end}" if start is not None else "не найден"
                print(f"  {index}. {CATEGORIES[key]} ({suggested})")
            while True:
                answer = input("Номер категории [0 — назад]: ").strip()
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
            else:
                print("Диапазон по заметкам не найден; задайте его вручную.")

        while True:
            value = input(
                f"Слайды [{default or 'обязательно'}; формат 1, 3-5]: "
            ).strip() or default
            try:
                return category, _parse_slide_ranges(value, total_slides)
            except ValueError as error:
                print(f"{error} Например: 1, 3-5, 8.")


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
    input_fn=input,
) -> bool:
    selected = ", ".join(
        str(first) if first == last else f"{first}-{last}"
        for first, last in ranges
    )
    count = sum(last - first + 1 for first, last in ranges)
    print(f"\nВыбрано: {selected} ({count} из {total_slides} слайдов)")
    print(f"Каталог категории: {CATEGORIES.get(category, category)}")
    while True:
        answer = input_fn("Продолжить? [Y — да, E — изменить, Q — отмена]: ")
        answer = answer.strip().lower()
        if answer in {"", "y", "yes", "д", "да"}:
            return True
        if answer in {"e", "edit", "и", "изменить"}:
            return False
        if answer in {"q", "quit", "cancel", "отмена"}:
            raise OperationCancelled
        print("Введите Y, E или Q.")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    return cleaned or "presentation"


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
        while True:
            category, ranges = _select_range(results, total_slides)
            if _confirm_selection(category, ranges, total_slides):
                break

        render_dir = temp_dir / "rendered"
        render_dir.mkdir()
        png_paths, _ = await convert_all_pngs(
            normalized, render_dir, config["quality"]
        )
        highest_slide = max(last for _, last in ranges)
        if highest_slide > len(png_paths):
            raise RuntimeError(
                f"В PDF {len(png_paths)} страниц, а выбран слайд {highest_slide}"
            )

        run_id = datetime.now().strftime("%H%M%S_%f")
        output_dir = (
            config["root_path"]
            / sunday.strftime("%Y-%m-%d")
            / _safe_name(source_path.stem)
            / run_id
            / category
        )
        output_dir.mkdir(parents=True, exist_ok=False)
        for first, last in ranges:
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
