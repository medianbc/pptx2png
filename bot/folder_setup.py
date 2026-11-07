"""Scheduled creation of the upcoming Sunday folder tree on Yandex Disk."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from pptx2png_core.structure import create_structure, load_template
from pptx2png_core.yandex_disk import (
    YandexDiskClient,
    date_folder_matches,
    get_nearest_sunday,
    month_folder_name,
    strip_disk_prefix,
)


async def ensure_sunday_folder_tree(
    client: YandexDiskClient,
    base_path: str,
    sunday: datetime,
    template_path: Path,
) -> str:
    """Create missing month, Sunday, and configured template folders."""
    structure = load_template(template_path)
    if structure is None or not structure:
        raise RuntimeError(f"Некорректный шаблон дерева папок: {template_path}")

    base_path = strip_disk_prefix(base_path).strip()
    if base_path != "/":
        base_path = base_path.rstrip("/")
    if not base_path or not await client.folder_exists(base_path):
        raise RuntimeError(
            f"Базовая папка Яндекс.Диска не найдена или недоступна: {base_path!r}"
        )

    month_name = month_folder_name(sunday)
    month = await client.find_child_folder(
        base_path,
        lambda name: " ".join(name.upper().split())
        == " ".join(month_name.upper().split()),
    )
    month_path = (
        strip_disk_prefix(month["path"])
        if month
        else f"{base_path.rstrip('/')}/{month_name}"
    )
    if not month and not await client.ensure_folder(month_path):
        raise RuntimeError(f"Не удалось создать папку месяца: {month_path}")

    date_folder = await client.find_child_folder(
        month_path,
        lambda name: date_folder_matches(name, sunday),
    )
    sunday_path = (
        strip_disk_prefix(date_folder["path"])
        if date_folder
        else f"{month_path}/{sunday:%d.%m.%Y}"
    )
    if not date_folder and not await client.ensure_folder(sunday_path):
        raise RuntimeError(f"Не удалось создать папку воскресенья: {sunday_path}")

    if not await create_structure(client, sunday_path, structure):
        raise RuntimeError(f"Не удалось создать дерево папок в {sunday_path}")

    logging.info("Дерево папок на воскресенье проверено: %s", sunday_path)
    return sunday_path


def should_check_folder_tree(
    now: datetime,
    last_successful_sunday: date | None,
    *,
    startup: bool,
    retry_pending: bool = False,
) -> bool:
    """Check at startup for catch-up, then once each Monday until successful."""
    if startup or retry_pending:
        return True
    sunday = get_nearest_sunday(now).date()
    return now.weekday() == 0 and last_successful_sunday != sunday


def seconds_until_next_monday(now: datetime) -> float:
    days_until_monday = (7 - now.weekday()) % 7
    next_monday = (now + timedelta(days=days_until_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if next_monday <= now:
        next_monday += timedelta(days=7)
    return (next_monday - now).total_seconds()


async def folder_setup_loop(
    client: YandexDiskClient,
    base_path: str,
    template_path: Path,
    interval_sec: int = 600,
) -> None:
    """Catch up on startup and retry the Monday setup until it succeeds."""
    last_successful_sunday: date | None = None
    startup = True
    retry_pending = False
    while True:
        now = datetime.now()
        sunday = get_nearest_sunday(now)
        should_check = should_check_folder_tree(
            now,
            last_successful_sunday,
            startup=startup,
            retry_pending=retry_pending,
        )
        if should_check:
            try:
                await ensure_sunday_folder_tree(
                    client, base_path, sunday, template_path
                )
                last_successful_sunday = sunday.date()
                retry_pending = False
            except Exception:
                retry_pending = True
                logging.exception(
                    "Не удалось создать дерево папок на воскресенье %s; "
                    "повтор через %s сек.",
                    sunday.strftime("%d.%m.%Y"),
                    interval_sec,
                )
        startup = False
        sleep_sec = (
            interval_sec if retry_pending else seconds_until_next_monday(now)
        )
        await asyncio.sleep(sleep_sec)
