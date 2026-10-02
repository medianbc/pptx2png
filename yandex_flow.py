# ==========================================
# yandex_flow.py — FACADE (v4.0, часть 3/3)
# ==========================================
# Тонкая обёртка: реэкспортирует публичный API из core и handlers.
# handlers.py импортирует из этого модуля — работает без изменений.
# ==========================================

from yandex_flow_core import (
    # утилиты
    _safe_answer,
    _safe_edit,
    _cat_order,
    _cat_meta,
    _cat_folder_path,
    _classify_slide,
    _count_category_slides,
    _detect_all_categories,
    _format_ranges_text,
    _normalize_item_ranges,
    _picker_is_active,
    _picker_status,
    _picker_has_live_tasks_locked,
    _ranges_to_start_end,
    _spawn_cancel_worker,
    _yd_stop_spinner,
    _yd_async_protected,
    _yd_run_protected,
    _yd_to_thread,
    # промпты
    _yd_claim_prompt,
    _yd_render_category_prompt,
    _yd_render_sermon_prompt,
    _yd_prompt_timeout_watchdog,
    # пайплайн
    _yd_prepare_files,
    _yd_convert_and_upload,
    _yd_cleanup_task,
    drain_deferred_cleanups,
)

from yandex_flow_handlers import router


# ==========================================
# ПУБЛИЧНЫЙ API (совместимость с handlers.py)
# ==========================================

# Имена, которые импортирует handlers.py:
#   from yandex_flow import (
#       router as yandex_router,
#       render_sermon_prompt,
#       cleanup_task as yd_cleanup_task,
#       is_sermon_slide,
#       prompt_timeout_watchdog,
#   )

render_sermon_prompt = _yd_render_sermon_prompt
render_category_prompt = _yd_render_category_prompt
convert_and_upload = _yd_convert_and_upload
cleanup_task = _yd_cleanup_task
claim_prompt = _yd_claim_prompt
prompt_timeout_watchdog = _yd_prompt_timeout_watchdog
drain_deferred_cleanups_async = drain_deferred_cleanups
safe_answer = _safe_answer


def is_sermon_slide(item: dict, slide_idx: int) -> bool:
    """Публичная обёртка. Импортирует из core (совместимость со старым API)."""
    from yandex_flow_core import _is_sermon_slide
    return _is_sermon_slide(item, slide_idx)


# ==========================================
# ЭКСПОРТ
# ==========================================

__all__ = [
    "router",
    "render_sermon_prompt",
    "render_category_prompt",
    "convert_and_upload",
    "cleanup_task",
    "claim_prompt",
    "prompt_timeout_watchdog",
    "drain_deferred_cleanups_async",
    "safe_answer",
    "is_sermon_slide",
]