"""Celery application."""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_shutdown

from books.config import get_settings
from books.logging import configure_logging
from books.workers.runner import shutdown_loop

settings = get_settings()

celery_app = Celery(
    "books",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["books.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Threads, not prefork: every task is IO-bound (Postgres, the aggregator,
    # the model) and shares one event loop per process (see runner.py), so
    # forking buys nothing and breaks on macOS' fork-unsafe frameworks.
    worker_pool="threads",
    worker_concurrency=4,
    result_expires=60 * 60 * 24,
    task_default_queue="books",
    # Emit task lifecycle events so Flower (`make flower`) can show live task
    # state rather than just worker presence — set here, not via a `-E` flag
    # on the worker command, so it applies no matter how the worker is
    # started (make worker, Docker, wherever).
    worker_send_task_events=True,
    task_send_sent_event=True,
)

configure_logging()

from books.workers.beat_schedule import BEAT_SCHEDULE  # noqa: E402  (needs celery_app configured)

celery_app.conf.beat_schedule = BEAT_SCHEDULE


@worker_process_shutdown.connect
def _close_loop(**_: object) -> None:
    shutdown_loop()
