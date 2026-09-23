"""Cursor encoding for the Teller adapter — no network involved.

Teller paginates per account with no delta signal across the whole
enrollment, so the cursor has three independent pieces of state (which
accounts are left, how far into the current one, and the incremental
watermark). These tests pin the encode/decode round trip down in isolation.
"""

from __future__ import annotations

from books.providers.teller import _Cursor


def test_no_prior_cursor_starts_a_fresh_walk() -> None:
    cursor = _Cursor.decode(None)
    assert cursor.account_ids is None
    assert cursor.from_id is None
    assert cursor.since is None


def test_a_mid_walk_cursor_round_trips_every_field() -> None:
    cursor = _Cursor(account_ids=["acc_2", "acc_3"], from_id="txn_99", since="2026-07-01")
    decoded = _Cursor.decode(cursor.encode())
    assert decoded.account_ids == ["acc_2", "acc_3"]
    assert decoded.from_id == "txn_99"
    assert decoded.since == "2026-07-01"


def test_a_resting_cursor_carries_only_the_watermark() -> None:
    cursor = _Cursor(account_ids=None, from_id=None, since="2026-07-15")
    decoded = _Cursor.decode(cursor.encode())
    assert decoded.account_ids is None
    assert decoded.from_id is None
    assert decoded.since == "2026-07-15"


def test_the_very_first_walk_has_no_watermark_yet() -> None:
    cursor = _Cursor(account_ids=["acc_1"], from_id=None, since=None)
    decoded = _Cursor.decode(cursor.encode())
    assert decoded.since is None
