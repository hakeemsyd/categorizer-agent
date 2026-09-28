"""Parsing what the model actually sends back.

Structured output is a contract the model mostly keeps. The cases here are the
ones it doesn't, observed live rather than imagined — a whole proposal handed
over as a string is not a reason to lose the proposal.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from books.agent.chart_builder import ChartProposal

ONE = {"name": "Travel", "account_type": "expense", "description": "Flights and hotels"}


def test_a_normal_proposal_parses() -> None:
    proposal = ChartProposal(categories=[ONE])  # type: ignore[list-item]
    assert proposal.categories[0].name == "Travel"


def test_the_whole_object_arriving_as_a_string_is_unwrapped() -> None:
    """Seen live on a 123-merchant business, mid-run."""
    proposal = ChartProposal.model_validate({"categories": json.dumps({"categories": [ONE]})})
    assert [c.name for c in proposal.categories] == ["Travel"]


def test_a_bare_list_as_a_string_is_unwrapped_too() -> None:
    proposal = ChartProposal.model_validate({"categories": json.dumps([ONE])})
    assert [c.name for c in proposal.categories] == ["Travel"]


def test_a_string_that_is_not_json_still_fails() -> None:
    """Tolerating a wrapper is not the same as accepting anything."""
    with pytest.raises(ValidationError):
        ChartProposal.model_validate({"categories": "sorry, I can't help with that"})


def test_an_account_type_outside_the_five_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ChartProposal.model_validate({"categories": [{**ONE, "account_type": "cost_of_goods"}]})
