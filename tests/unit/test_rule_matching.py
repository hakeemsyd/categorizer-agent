from __future__ import annotations

import uuid
from datetime import UTC, datetime

from books.core.categorization import match_rule
from books.core.models import Rule, Transaction


def _rule(match_type: str, pattern: str, priority: int = 100) -> Rule:
    return Rule(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        business_id=uuid.uuid4(),
        match_type=match_type,
        pattern=pattern,
        category_id=uuid.uuid4(),
        priority=priority,
        created_at=datetime.now(UTC),
    )


def _tx(vendor: str | None = None, description: str = "") -> Transaction:
    return Transaction(vendor=vendor, description=description)


def test_vendor_equals_is_case_insensitive() -> None:
    rule = _rule("vendor_equals", "AWS")
    assert match_rule(_tx(vendor="aws"), [rule]) is rule


def test_vendor_equals_does_not_match_a_substring() -> None:
    assert match_rule(_tx(vendor="AWS Marketplace"), [_rule("vendor_equals", "AWS")]) is None


def test_vendor_contains_matches_a_substring() -> None:
    rule = _rule("vendor_contains", "gusto")
    assert match_rule(_tx(vendor="GUSTO PAYROLL INC"), [rule]) is rule


def test_description_contains_matches() -> None:
    rule = _rule("description_contains", "ach credit")
    assert match_rule(_tx(description="ACME CORP ACH CREDIT"), [rule]) is rule


def test_lowest_priority_number_wins() -> None:
    specific = _rule("vendor_contains", "aws", priority=10)
    general = _rule("vendor_contains", "a", priority=50)
    assert match_rule(_tx(vendor="AWS"), [general, specific]) is specific


def test_no_match_returns_none() -> None:
    assert match_rule(_tx(vendor="Unknown Vendor"), [_rule("vendor_equals", "AWS")]) is None


def test_missing_vendor_does_not_crash() -> None:
    assert match_rule(_tx(vendor=None), [_rule("vendor_contains", "aws")]) is None
