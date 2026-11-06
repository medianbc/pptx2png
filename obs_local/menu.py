"""Interactive menu for local OBS preparation workflows."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable

from . import app, prepare_songs


def _prepare_presentation() -> None:
    try:
        asyncio.run(app.run())
    except app.OperationCancelled:
        print("Подготовка презентации отменена.")
    except Exception as error:
        print(f"Ошибка подготовки презентации: {error}")


def _prepare_songs() -> None:
    try:
        config = prepare_songs.load_config()
        prepare_songs.setup_logging(config["log_dir"])
        prepare_songs.LOGGER.info(
            "Начало локальной подготовки песен; подробный лог: %s",
            config["log_dir"] / "debug.log",
        )
        asyncio.run(
            prepare_songs.run(
                prepare_songs.get_nearest_sunday().date(),
                config=config,
                interactive=True,
            )
        )
    except Exception as error:
        if logging.getLogger().handlers:
            prepare_songs.LOGGER.exception(
                "Подготовка песен завершилась ошибкой"
            )
        print(f"Ошибка подготовки песен: {error}")


def run_menu(input_fn: Callable[[str], str] = input) -> None:
    actions = {
        "1": ("Подготовить презентацию", _prepare_presentation),
        "2": ("Подготовить песни", _prepare_songs),
    }

    while True:
        print("\nЛокальная подготовка материалов для OBS")
        for key, (label, _) in actions.items():
            print(f"  {key}. {label}")
        print("  0. Выход")

        try:
            choice = input_fn("Выберите действие: ").strip().lower()
        except EOFError:
            print("\nВыход.")
            return

        if choice in {"0", "q", "quit", "exit", "выход"}:
            return
        action = actions.get(choice)
        if action is None:
            print("Введите 1, 2 или 0.")
            continue
        action[1]()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Интерактивное меню подготовки материалов для OBS."
    )
    parser.add_argument(
        "--date",
        help="Запустить подготовку презентации на дату YYYY-MM-DD без меню.",
    )
    args = parser.parse_args()
    if args.date is not None:
        app.main()
        return
    run_menu()
