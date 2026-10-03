"""Локальный пайплайн: Яндекс.Диск -> заметки -> PNG для OBS."""

from __future__ import annotations

import asyncio
import configparser
import re
import shutil
import tempfile
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


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
CATEGORIES = {"sermon": "Проповедь", "opening": "Начало", "prayer": "Молитва"}


def load_config() -> dict[str, Any]:
    settings = configparser.ConfigParser(interpolation=None)
    secrets = configparser.ConfigParser(interpolation=None)
    settings_path = BASE_DIR / "settings.ini"
    secrets_path = BASE_DIR / "config.ini"
    if not secrets_path.exists():
        secrets_path = PROJECT_DIR / "config.ini"

    if not settings.read(settings_path, encoding="utf-8"):
        raise RuntimeError(f"Не найден файл настроек: {settings_path}")
    if not secrets.read(secrets_path, encoding="utf-8"):
        raise RuntimeError(f"Не найден файл секретов: {secrets_path}")

    token = secrets.get("YandexDisk", "token", fallback="").strip()
    base_path = settings.get("YandexDisk", "base_path", fallback="").strip()
    source_folder = settings.get(
        "YandexDisk", "source_folder", fallback="Служение"
    ).strip()
    root_value = settings.get(
        "OBS Local paths", "root_path", fallback=""
    ).strip()
    quality = settings.get(
        "OBS Local paths", "quality", fallback="standard"
    ).strip().lower()

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

    legacy_keyword = settings.get(
        "YandexDisk", "sermon_keyword", fallback="проповед"
    )
    raw_keywords = {
        "sermon": settings.get(
            "SlideCategories", "sermon_keywords", fallback=legacy_keyword
        ),
        "opening": settings.get(
            "SlideCategories", "opening_keywords", fallback="начало,в начале"
        ),
        "prayer": settings.get(
            "SlideCategories", "prayer_keywords", fallback="молитва,молиться"
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
    config: dict[str, Any], sunday: datetime, temp_dir: Path
) -> Path:
    async with aiohttp.ClientSession() as http_session:
        client = YandexDiskClient(config["token"], http_session)
        accessible, error = await client.check_access()
        if not accessible:
            raise RuntimeError(f"Нет доступа к Яндекс.Диску: {error}")

        paths = await resolve_sunday_paths(
            client,
            config["base_path"],
            sunday,
            source_folder=config["source_folder"],
        )
        if not paths:
            raise RuntimeError(f"Не найдена папка служения за {sunday:%d.%m.%Y}")

        files = await find_pptx_in_source(client, paths["source"], sunday)
        if not files:
            raise RuntimeError(
                f"В {paths['source']} не найдены PPT/PPTX за {sunday:%d.%m.%Y}"
            )

        print(f"\nПрезентации на {sunday:%d.%m.%Y}:")
        for index, item in enumerate(files, start=1):
            print(f"  {index}. {item['name']}")

        while True:
            try:
                choice = int(input("Номер презентации: ").strip()) - 1
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
) -> tuple[str, int, int]:
    print("\nВыберите: all, sermon, opening или prayer.")
    while True:
        category = input("Категория [all]: ").strip().lower() or "all"
        if category == "all" or category in CATEGORIES:
            break
        print("Неизвестная категория.")

    if category == "all":
        return category, 1, total_slides

    start, end, _ = results[category]
    default = f"{start}-{end}" if start is not None and end is not None else ""
    if not default:
        print("Диапазон по заметкам не найден; введите его вручную.")

    while True:
        value = input(f"Диапазон [{default}]: ").strip() or default
        match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", value)
        if match:
            first, last = map(int, match.groups())
            if 1 <= first <= last <= total_slides:
                return category, first, last
        print(f"Введите диапазон от 1 до {total_slides}, например 5-30.")


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
        category, first, last = _select_range(results, total_slides)

        render_dir = temp_dir / "rendered"
        render_dir.mkdir()
        png_paths, _ = await convert_all_pngs(
            normalized, render_dir, config["quality"]
        )
        if last > len(png_paths):
            raise RuntimeError(
                f"В PDF {len(png_paths)} страниц, а выбран диапазон до {last}"
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
        for png_path in png_paths[first - 1 : last]:
            shutil.copy2(png_path, output_dir / png_path.name)
        return output_dir


async def run(date: datetime | None = None) -> Path:
    config = load_config()
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
    except Exception as error:
        parser.exit(1, f"Ошибка: {error}\n")
