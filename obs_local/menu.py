"""Interactive menu for local OBS preparation workflows."""

from __future__ import annotations

import argparse
import asyncio
import configparser
import getpass
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp
from pptx2png_core.yandex_disk import YandexDiskClient

from . import app, prepare_songs, updater
from .config_utils import get_settings_path, update_ini_file


def _settings_menu(input_fn: Callable[[str], str]) -> bool:
    while True:
        try:
            entries = []
            try:
                path = get_settings_path(app.BASE_DIR)
                settings = configparser.ConfigParser(interpolation=None)
                with path.open(encoding="utf-8") as source:
                    settings.read_file(source)
                entries = [
                    (section, key, value)
                    for section in settings.sections()
                    for key, value in settings.items(section)
                    if key.lower() != "token"
                ]
                legacy_url = settings.get("Song preparation", "program_url", fallback="")
                default_cloud = prepare_songs._legacy_program_cloud(legacy_url)
                for key, default in (
                    ("priority_cloud_disk", default_cloud),
                    ("program_google_url", ""),
                    ("program_yandex_url", ""),
                ):
                    if not settings.has_option("Song preparation", key):
                        entries.append(("Song preparation", key, default))
            except (configparser.Error, OSError, UnicodeError) as error:
                print(f"Не удалось прочитать настройки: {error}")
            print("\nНастройки OBS")
            for index, (section, key, value) in enumerate(entries, 1):
                print(f"  {index}. [{section}] {key} = {value}")
            token_choice = str(len(entries) + 1)
            update_choice = str(len(entries) + 2)
            print(f"  {token_choice}. Ввести токен Яндекс.Диска")
            print(f"  {update_choice}. Проверить обновления")
            print("  0. Назад")
            choice = input_fn("Параметр [0]: ").strip() or "0"
            if choice == "0":
                return False
            if choice == update_choice:
                if _check_updates(input_fn):
                    return True
                continue
            if choice == token_choice:
                token = getpass.getpass("OAuth-токен (ввод скрыт): ").strip()
                if not token or "\n" in token or "\r" in token:
                    raise ValueError("Токен должен быть непустой строкой.")
                update_ini_file(
                    app.PROJECT_DIR / "config.ini",
                    {"YandexDisk": {"token": token}},
                    file_mode=0o600,
                )
                local_config = app.BASE_DIR / "config.ini"
                if local_config.is_file():
                    update_ini_file(
                        local_config,
                        {"YandexDisk": {"token": token}},
                        file_mode=0o600,
                    )
                print("Токен сохранён.")
                continue
            if not choice.isdecimal() or not 1 <= int(choice) <= len(entries):
                print("Введите номер из списка.")
                continue
            section, key, old_value = entries[int(choice) - 1]
            if key == "priority_cloud_disk":
                print("  1. Google\n  2. Яндекс")
            value = input_fn(f"Новое значение [{old_value}] (Enter — оставить): ")
            if not value.strip():
                continue
            value = value.strip()
            if key == "priority_cloud_disk":
                value = {"1": "google", "2": "yandex"}.get(value.lower(), value.lower())
                if value not in {"google", "yandex"}:
                    raise ValueError("Выберите 1 (Google) или 2 (Яндекс).")
            if "\n" in value or "\r" in value:
                raise ValueError("Значение должно занимать одну строку.")
            if key == "quality" and value not in {"standard", "2k", "4k"}:
                raise ValueError("Качество: standard, 2k или 4k.")
            if key == "cache_missing_yandex_song_zips":
                if value.lower() not in configparser.ConfigParser.BOOLEAN_STATES:
                    raise ValueError("Укажите true или false.")
            if key == "program_url":
                prepare_songs._normalize_program_public_key(value)
            if key in {"program_google_url", "program_yandex_url"}:
                prepare_songs._validate_program_cloud_url(
                    value, "google" if key == "program_google_url" else "yandex",
                )
            update_ini_file(path, {section: {key: value}})
            print("Настройка сохранена.")
        except EOFError:
            return False
        except configparser.Error as error:
            print(f"Не удалось прочитать настройки: {error}")
            return False
        except OSError as error:
            print(f"Не удалось изменить настройки: {error}")
            return False
        except (ValueError, RuntimeError) as error:
            print(f"Не удалось изменить настройки: {error}")


async def _update_song_library(config: dict[str, Any]) -> None:
    if not config["yandex_token"]:
        raise RuntimeError("Для обновления библиотеки нужен токен Яндекс.Диска.")
    if not config["yandex_zip_png_paths"] and not config["yandex_pptx_paths"]:
        raise RuntimeError("Не заданы каталоги песен на Яндекс.Диске.")
    with tempfile.TemporaryDirectory(prefix="song_library_") as directory:
        async with aiohttp.ClientSession() as session:
            client = YandexDiskClient(config["yandex_token"], session)
            cached, failed = await prepare_songs._cache_remote_song_zips(
                client, config, Path(directory)
            )
    print(f"Обновление библиотеки: создано ZIP {cached}, ошибок {failed}.")
    if failed:
        raise RuntimeError("Некоторые материалы не обработаны; подробности в журнале.")


def _songs_menu(input_fn: Callable[[str], str], *, debug: bool) -> None:
    while True:
        print(
            "\nПодготовка песен\n  1. Прочитать программу\n"
            "  2. Обновить локальную папку песен\n  0. Назад"
        )
        try:
            choice = input_fn("Действие [0]: ").strip() or "0"
        except EOFError:
            return
        if choice == "0":
            return
        if choice == "1":
            _prepare_songs(console_debug=debug)
        elif choice == "2":
            try:
                config = prepare_songs.load_config()
                prepare_songs.setup_logging(config["log_dir"], console_debug=debug)
                asyncio.run(_update_song_library(config))
            except Exception as error:
                prepare_songs.log_user_error(error, "Ошибка обновления библиотеки")
                print(f"Ошибка обновления библиотеки: {error}")
        else:
            print("Введите 1, 2 или 0.")


def _prepare_presentation() -> None:
    try:
        asyncio.run(app.run())
    except app.OperationCancelled:
        print("Подготовка презентации отменена.")
    except Exception as error:
        print(f"Ошибка подготовки презентации: {error}")


def _prepare_songs(console_debug: bool = False) -> None:
    log_dir = None
    try:
        config = prepare_songs.load_config()
        log_dir = config["log_dir"]
        prepare_songs.setup_logging(log_dir, console_debug=console_debug)
        prepare_songs.LOGGER.info(
            "Начало локальной подготовки песен; подробный лог: %s",
            log_dir / "debug.log",
        )
        asyncio.run(
            prepare_songs.run(
                prepare_songs.get_nearest_sunday().date(),
                config=config,
                interactive=True,
            )
        )
    except prepare_songs.OperationCancelled:
        print("Подготовка песен отменена.")
    except Exception as error:
        if logging.getLogger().handlers:
            prepare_songs.log_user_error(
                error, "Подготовка песен завершилась ошибкой"
            )
        print(
            "Ошибка подготовки песен: "
            f"{prepare_songs.format_user_error(error)}"
        )
        if log_dir is not None:
            print(f"Подробный журнал: {log_dir / 'debug.log'}")


def _check_updates(input_fn: Callable[[str], str]) -> bool:
    try:
        update = updater.check_for_updates()
    except updater.UpdateError as error:
        print(f"Не удалось проверить обновления: {error}")
        return False

    if not update.available:
        print("Установлена последняя версия.")
        return False

    if update.local_revision is None:
        print(
            "Доступно обновление. Версию текущей архивной установки "
            "определить не удалось."
        )
    else:
        print(
            "Доступно обновление: "
            f"{update.local_revision[:7]} → {update.remote_revision[:7]} "
            f"(ветка {update.branch})."
        )
    try:
        answer = input_fn("1. Установить обновление\n0. Отмена\nВаш выбор [0]: ")
    except EOFError:
        print("Обновление отменено.")
        return False
    if answer.strip() != "1":
        print("Обновление отменено.")
        return False

    try:
        updater.install_update(update)
    except updater.UpdateError as error:
        print(f"Не удалось установить обновление: {error}")
        return False
    print(
        "Обновление и проверка Python-зависимостей завершены. "
        "Перезапустите локальный сценарий, чтобы загрузить новую версию."
    )
    return True


def run_menu(
    input_fn: Callable[[str], str] = input,
    *,
    debug: bool = False,
) -> None:
    actions = {
        "1": ("Подготовить презентацию", _prepare_presentation),
        "2": ("Подготовить песни", _prepare_songs),
    }

    while True:
        print("\nЛокальная подготовка материалов для OBS")
        for key, (label, _) in actions.items():
            print(f"  {key}. {label}")
        print("  3. Настройки")
        print("  0. Выход")

        try:
            choice = input_fn("Выберите действие: ").strip().lower()
        except EOFError:
            print("\nВыход.")
            return

        if choice == "0" or not choice:
            return
        if choice == "3":
            if _settings_menu(input_fn):
                return
            continue
        action = actions.get(choice)
        if action is None:
            print("Введите 1, 2, 3 или 0.")
            continue
        if choice == "2":
            _songs_menu(input_fn, debug=debug)
        else:
            action[1]()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Интерактивное меню подготовки материалов для OBS."
    )
    parser.add_argument(
        "--date",
        help="Запустить подготовку презентации на дату YYYY-MM-DD без меню.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Показывать подробный DEBUG-лог при подготовке песен из меню."
        ),
    )
    args = parser.parse_args()
    if args.date is not None:
        if args.debug:
            parser.error("--debug применяется к подготовке песен из меню.")
        app.main()
        return
    run_menu(debug=args.debug)
