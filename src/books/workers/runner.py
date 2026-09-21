"""One long-lived event loop per worker process.

Celery tasks are synchronous but the core is async. Spinning up a fresh
``asyncio.run`` per task would discard the asyncpg pool every time (and bind
pooled connections to a dead loop), so the process keeps a single loop on a
daemon thread and hands coroutines to it.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="books-worker-loop", daemon=True)
        thread.start()
        _loop = loop
        return loop


def run_async[T](coro: Coroutine[Any, Any, T], *, timeout: float | None = 600) -> T:
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop()).result(timeout)


def shutdown_loop() -> None:
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            return
        _loop.call_soon_threadsafe(_loop.stop)
        _loop = None
