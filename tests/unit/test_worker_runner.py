"""The worker's shared event loop — the piece most likely to break quietly."""

from __future__ import annotations

import asyncio

from books.workers.runner import run_async, shutdown_loop


def test_run_async_returns_the_result() -> None:
    async def work() -> int:
        await asyncio.sleep(0)
        return 42

    assert run_async(work()) == 42


def test_successive_calls_share_one_loop() -> None:
    """A fresh loop per task would strand the asyncpg pool on a dead loop."""

    async def loop_id() -> int:
        return id(asyncio.get_running_loop())

    assert run_async(loop_id()) == run_async(loop_id())


def test_exceptions_propagate_to_the_caller() -> None:
    async def boom() -> None:
        raise ValueError("kaboom")

    try:
        run_async(boom())
    except ValueError as exc:
        assert str(exc) == "kaboom"
    else:  # pragma: no cover
        raise AssertionError("exception did not propagate")


def test_loop_restarts_after_shutdown() -> None:
    async def ping() -> str:
        return "pong"

    run_async(ping())
    shutdown_loop()
    assert run_async(ping()) == "pong"
