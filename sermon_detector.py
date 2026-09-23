# ==========================================
# sermon_detector.py — поиск диапазона проповеди
# ==========================================

import logging
from typing import Dict, Optional, Tuple, List


def find_sermon_range(
    notes: Dict[int, str],
    keyword: str,
) -> Tuple[Optional[int], Optional[int], List[int]]:
    """
    Ищет диапазон проповеди по ключевому слову в заметках.

    :return: (start, end, all_matches)
             - (None, None, []) — не найдено
             - (N, N, [N])     — одно совпадение (нужен ручной ввод)
             - (start, end, [...]) — несколько совпадений
    """
    if not notes:
        return None, None, []
    if not keyword or not keyword.strip():
        logging.error("find_sermon_range: пустой keyword — пропускаем поиск")
        return None, None, []
        
    keyword_lower = keyword.lower()
    matches = []

    for slide_num, text in sorted(notes.items()):
        if keyword_lower in text.lower():
            matches.append(slide_num)

    if not matches:
        return None, None, []

    start = min(matches)
    end = max(matches)
    return start, end, matches


def format_sermon_message(
    start: Optional[int],
    end: Optional[int],
    matches: List[int],
    total_slides: int,
) -> str:
    """Форматирует сообщение для пользователя."""
    if not matches:
        return (
            f"🔍 <b>Пометка «проповедь» не найдена в заметках.</b>\n\n"
            f"Всего слайдов: {total_slides}"
        )

    if len(matches) == 1:
        return (
            f"⚠️ <b>Найдено только одно совпадение</b> (слайд {matches[0]}).\n\n"
            f"Всего слайдов: {total_slides}\n"
            f"Укажите диапазон вручную — например, <code>5-30</code>."
        )

    preview = ", ".join(str(n) for n in matches[:10])
    if len(matches) > 10:
        preview += f" …и ещё {len(matches) - 10}"

    return (
        f"🎯 <b>Найдена пометка «проповедь» на слайдах:</b>\n"
        f"<code>{preview}</code>\n\n"
        f"📊 Предлагаемый диапазон: <b>{start}–{end}</b>\n"
        f"Всего слайдов: {total_slides}"
    )
