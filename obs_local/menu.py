"""Interactive menu for local OBS preparation workflows."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable

from . import app, prepare_songs, updater


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
        answer = input_fn("Установить обновление и проверить зависимости? [y/N]: ")
    except EOFError:
        print("Обновление отменено.")
        return False
    if answer.strip().lower() not in {"y", "yes", "д", "да"}:
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
        print("  3. Проверить обновления")
        print("  0. Выход")

        try:
            choice = input_fn("Выберите действие: ").strip().lower()
        except EOFError:
            print("\nВыход.")
            return

        if choice in {"0", "q", "quit", "exit", "выход"}:
            return
        if choice == "3":
            if _check_updates(input_fn):
                return
            continue
        action = actions.get(choice)
        if action is None:
            print("Введите 1, 2, 3 или 0.")
            continue
        if choice == "2":
            _prepare_songs(console_debug=debug)
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
