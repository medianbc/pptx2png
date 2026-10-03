# ==========================================
# sermon_detector.py — поиск диапазонов по категориям
# ==========================================

import logging
from typing import Dict, Optional, Tuple, List


def find_category_range(
    notes: Dict[int, str],
    keywords,
) -> Tuple[Optional[int], Optional[int], List[int]]:
    """
    Обобщённый поиск: слайды, в заметках которых есть ХОТЯ БЫ ОДНО
    из ключевых слов.

    :param notes:    {номер_слайда: текст_заметок}
    :param keywords: str | list[str] — одно ключевое слово или список.
    :return: (start, end, all_matches)
             - (None, None, []) — не найдено
             - (N, N, [N])      — одно совпадение
             - (start, end, [...]) — несколько совпадений
    """
    if not notes:
        return None, None, []

    # ✅ Нормализуем в список ключевых слов
    if isinstance(keywords, str):
        kws = [keywords]
    elif isinstance(keywords, (list, tuple, set)):
        kws = [str(k) for k in keywords]
    else:
        kws = []

    kws = [k.strip().lower() for k in kws if k and k.strip()]

    if not kws:
        logging.debug("find_category_range: пустой список keywords")
        return None, None, []

    matches: List[int] = []
    for slide_num, text in sorted(notes.items()):
        if not text:
            continue
        text_lower = text.lower()
        if any(kw in text_lower for kw in kws):
            matches.append(slide_num)

    if not matches:
        return None, None, []

    start = min(matches)
    end = max(matches)
    logging.debug(
        f"find_category_range: keywords={kws!r} matches={matches} "
        f"start={start} end={end}"
    )
    return start, end, matches


def find_sermon_range(
    notes: Dict[int, str],
    keyword,
) -> Tuple[Optional[int], Optional[int], List[int]]:
    """
    Legacy-обёртка для обратной совместимости.
    Используется в старых вызовах.
    """
    return find_category_range(notes, keyword)


def format_sermon_message(
    start: Optional[int],
    end: Optional[int],
    matches: List[int],
    total_slides: int,
) -> str:
    """Форматирует сообщение для пользователя (legacy)."""
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