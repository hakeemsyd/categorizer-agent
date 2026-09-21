"""Selecting which business the CLI works on."""

from __future__ import annotations

import pytest
import typer

from books.faces.cli import context
from books.faces.cli.commands import _resolve

LOCAL = "http://localhost:8000"
OTHER = "https://books.example.com"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(context.ENV_VAR, raising=False)
    return tmp_path


class StubClient:
    """Just enough of BooksClient for the resolution chain."""

    def __init__(self, businesses: list[dict], base_url: str = LOCAL) -> None:
        self._businesses = businesses
        self.base_url = base_url

    def list_businesses(self) -> list[dict]:
        return self._businesses


CRAFTS = {"id": "11111111-1111-1111-1111-111111111111", "name": "Coding Crafts"}
STUDIO = {"id": "22222222-2222-2222-2222-222222222222", "name": "Side Studio"}


# --- storage -------------------------------------------------------------


def test_nothing_is_selected_to_begin_with() -> None:
    assert context.get_business(LOCAL) is None


def test_selection_round_trips() -> None:
    context.set_business(LOCAL, business_id=CRAFTS["id"], name=CRAFTS["name"])
    selected = context.get_business(LOCAL)
    assert selected is not None
    assert (selected.id, selected.name) == (CRAFTS["id"], CRAFTS["name"])


def test_selection_is_per_deployment() -> None:
    """A business id from localhost must not leak into a production server."""
    context.set_business(LOCAL, business_id=CRAFTS["id"], name=CRAFTS["name"])
    assert context.get_business(OTHER) is None

    context.set_business(OTHER, business_id=STUDIO["id"], name=STUDIO["name"])
    assert context.get_business(LOCAL).id == CRAFTS["id"]
    assert context.get_business(OTHER).id == STUDIO["id"]


def test_trailing_slashes_are_the_same_deployment() -> None:
    context.set_business(LOCAL + "/", business_id=CRAFTS["id"], name=CRAFTS["name"])
    assert context.get_business(LOCAL) is not None


def test_clearing_reports_whether_anything_was_set() -> None:
    assert context.clear_business(LOCAL) is False
    context.set_business(LOCAL, business_id=CRAFTS["id"], name=CRAFTS["name"])
    assert context.clear_business(LOCAL) is True
    assert context.get_business(LOCAL) is None


def test_a_corrupt_config_is_ignored_rather_than_fatal(isolated_config) -> None:
    path = context.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert context.get_business(LOCAL) is None
    # ...and writing recovers the file rather than compounding the problem.
    context.set_business(LOCAL, business_id=CRAFTS["id"], name=CRAFTS["name"])
    assert context.get_business(LOCAL).id == CRAFTS["id"]


# --- resolution order ----------------------------------------------------


def test_a_single_business_needs_no_selection() -> None:
    assert _resolve.business(StubClient([CRAFTS]), None) == CRAFTS


def test_several_businesses_without_a_selection_stops_with_guidance(capsys) -> None:
    with pytest.raises(typer.Exit):
        _resolve.business(StubClient([CRAFTS, STUDIO]), None)
    message = capsys.readouterr().err
    assert "books business use" in message
    assert "Coding Crafts" in message and "Side Studio" in message


def test_the_selection_is_used_when_no_flag_is_given() -> None:
    context.set_business(LOCAL, business_id=STUDIO["id"], name=STUDIO["name"])
    assert _resolve.business(StubClient([CRAFTS, STUDIO]), None) == STUDIO


def test_the_flag_beats_the_selection() -> None:
    context.set_business(LOCAL, business_id=STUDIO["id"], name=STUDIO["name"])
    assert _resolve.business(StubClient([CRAFTS, STUDIO]), "Coding Crafts") == CRAFTS


def test_the_env_var_beats_the_selection(monkeypatch) -> None:
    context.set_business(LOCAL, business_id=STUDIO["id"], name=STUDIO["name"])
    monkeypatch.setenv(context.ENV_VAR, "Coding Crafts")
    assert _resolve.business(StubClient([CRAFTS, STUDIO]), None) == CRAFTS


def test_the_flag_beats_the_env_var(monkeypatch) -> None:
    monkeypatch.setenv(context.ENV_VAR, "Side Studio")
    assert _resolve.business(StubClient([CRAFTS, STUDIO]), "Coding Crafts") == CRAFTS


def test_a_business_can_be_named_by_id_or_by_name() -> None:
    client = StubClient([CRAFTS, STUDIO])
    assert _resolve.business(client, CRAFTS["id"]) == CRAFTS
    assert _resolve.business(client, "coding crafts") == CRAFTS


def test_an_unknown_name_lists_what_exists(capsys) -> None:
    with pytest.raises(typer.Exit):
        _resolve.business(StubClient([CRAFTS, STUDIO]), "Nope Ltd")
    assert "Coding Crafts" in capsys.readouterr().err


def test_a_stale_selection_is_reported_not_silently_replaced(capsys) -> None:
    """The business was deleted since it was chosen — do not act on another."""
    context.set_business(LOCAL, business_id=STUDIO["id"], name=STUDIO["name"])
    with pytest.raises(typer.Exit):
        _resolve.business(StubClient([CRAFTS]), None)
    assert "no longer exists" in capsys.readouterr().err


def test_no_businesses_at_all_points_at_init(capsys) -> None:
    with pytest.raises(typer.Exit):
        _resolve.business(StubClient([]), None)
    assert "books init" in capsys.readouterr().err
