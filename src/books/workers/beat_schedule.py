"""Periodic safety net (plan.md §5, step 4).

Webhooks are the fast path; this catches items whose webhook never arrived.
A sync with an unchanged cursor is a cheap no-op.
"""

from __future__ import annotations

from celery.schedules import crontab

from books.config import get_settings

_settings = get_settings()

BEAT_SCHEDULE = {
    "sync-all-items": {
        "task": "books.sync_all_items",
        "schedule": crontab(minute=f"*/{max(1, min(_settings.sync_all_items_minutes, 59))}")
        if _settings.sync_all_items_minutes < 60
        else crontab(minute=7),  # hourly, off the hour to avoid thundering herd
    },
}
