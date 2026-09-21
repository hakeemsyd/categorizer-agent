"""The API face: routing, auth, error mapping, and what it hands to workers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from books.core import repository as repo

pytestmark = pytest.mark.db


async def test_health_reports_the_database_and_providers(api_client):
    response = await api_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert "fake" in body["providers"]


async def test_endpoints_require_the_bearer_token(api_client):
    response = await api_client.get("/businesses", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


async def test_webhooks_do_not_require_the_bearer_token(api_client, linked_item, dispatcher):
    """Providers authenticate by signature, not by our shared secret."""
    response = await api_client.post(
        f"/webhooks/{linked_item.provider}",
        json={"item_id": linked_item.provider_item_id, "event_type": "sync_available"},
        headers={"Authorization": ""},
    )
    assert response.status_code == 200
    assert response.json()["detail"] == "Sync queued"
    assert [name for name, _ in dispatcher.calls] == ["sync_item"]


async def test_webhook_for_an_unknown_item_is_acknowledged_not_errored(api_client, dispatcher):
    response = await api_client.post("/webhooks/fake", json={"item_id": "nope"})
    assert response.status_code == 200
    assert "Unknown item" in response.json()["detail"]
    assert dispatcher.calls == []


async def test_item_error_webhook_marks_the_item(api_client, session, linked_item):
    response = await api_client.post(
        "/webhooks/fake",
        json={"item_id": linked_item.provider_item_id, "event_type": "item_error"},
    )
    assert response.status_code == 200
    # Same session, so the route's mutation is visible on the identity-mapped row.
    assert linked_item.status == "error"


async def test_missing_row_maps_to_404(api_client):
    response = await api_client.get("/transactions/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"] == "NotFoundError"


async def test_sync_queues_by_default_and_runs_inline_on_wait(api_client, linked_item, dispatcher):
    queued = await api_client.post("/sync", json={"item_id": str(linked_item.id)})
    assert queued.status_code == 200
    assert [name for name, _ in dispatcher.calls] == ["sync_item"]

    dispatcher.calls.clear()
    inline = await api_client.post("/sync", json={"item_id": str(linked_item.id), "wait": True})
    body = inline.json()["results"][0]
    assert body["inserted"] == 5
    # Every newly ingested transaction is queued for categorization (plan.md §5.5).
    assert [name for name, _ in dispatcher.calls] == ["categorize_transaction"] * 5


async def test_transaction_filters(api_client, linked_item):
    await api_client.post("/sync", json={"item_id": str(linked_item.id), "wait": True})

    everything = await api_client.get(
        "/transactions", params={"business_id": str(linked_item.business_id)}
    )
    assert everything.json()["total"] == 5

    uncategorized = await api_client.get(
        "/transactions",
        params={"business_id": str(linked_item.business_id), "uncategorized": True},
    )
    assert uncategorized.json()["total"] == 5

    searched = await api_client.get(
        "/transactions", params={"business_id": str(linked_item.business_id), "search": "gusto"}
    )
    assert searched.json()["total"] == 1
    assert searched.json()["items"][0]["vendor"] == "Gusto"


async def test_recategorize_by_name_marks_reviewed_and_records_the_actor(
    api_client, session, business, chart_of_accounts, linked_item
):
    await api_client.post("/sync", json={"item_id": str(linked_item.id), "wait": True})
    page = await api_client.get(
        "/transactions", params={"business_id": str(business.id), "search": "aws"}
    )
    transaction_id = page.json()["items"][0]["id"]

    response = await api_client.post(
        f"/transactions/{transaction_id}/recategorize",
        json={"category_name": "Software & Subscriptions", "actor": "cli:hakeem", "note": "infra"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["needs_review"] is False
    assert body["last_reviewed_at"] is not None

    history = await api_client.get(f"/transactions/{transaction_id}/history")
    assert history.json()[0]["actor"] == "cli:hakeem"
    assert history.json()[0]["rationale"] == "infra"


async def test_recategorize_to_an_unknown_category_is_rejected(
    api_client, business, chart_of_accounts, linked_item
):
    await api_client.post("/sync", json={"item_id": str(linked_item.id), "wait": True})
    page = await api_client.get("/transactions", params={"business_id": str(business.id)})
    transaction_id = page.json()["items"][0]["id"]

    response = await api_client.post(
        f"/transactions/{transaction_id}/recategorize",
        json={"category_name": "Nonexistent", "actor": "cli:hakeem"},
    )
    assert response.status_code == 422
    assert "chart of accounts" in response.json()["detail"]


async def test_category_and_rule_lifecycle(api_client, business):
    created = await api_client.post(
        f"/businesses/{business.id}/categories",
        json={"name": "Travel", "account_type": "expense", "description": "Flights and hotels"},
    )
    assert created.status_code == 201
    category_id = created.json()["id"]

    rule = await api_client.post(
        "/rules",
        json={
            "business_id": str(business.id),
            "match_type": "vendor_contains",
            "pattern": "Delta",
            "category_name": "Travel",
            "created_by": "cli:hakeem",
        },
    )
    assert rule.status_code == 201
    assert rule.json()["category_id"] == category_id

    listed = await api_client.get("/rules", params={"business_id": str(business.id)})
    assert len(listed.json()) == 1

    removed = await api_client.delete(f"/rules/{rule.json()['id']}")
    assert removed.json()["active"] is False
    assert (await api_client.get("/rules", params={"business_id": str(business.id)})).json() == []


async def test_archiving_a_category_keeps_it_out_of_the_default_list(
    api_client, business, session, linked_item
):
    created = await api_client.post(
        f"/businesses/{business.id}/categories",
        json={"name": "Obsolete", "account_type": "expense"},
    )
    await api_client.delete(f"/categories/{created.json()['id']}")

    active = await api_client.get(f"/businesses/{business.id}/categories")
    assert active.json() == []
    archived = await api_client.get(
        f"/businesses/{business.id}/categories", params={"include_archived": True}
    )
    assert [c["name"] for c in archived.json()] == ["Obsolete"]

    # ...and the agent may not write to it.
    from books.core.categorization import apply_category
    from books.core.errors import ValidationError

    account = (await repo.list_accounts(session, business_id=business.id))[0]
    transaction_id, _ = await repo.upsert_transaction(
        session,
        values={
            "tenant_id": business.tenant_id,
            "business_id": business.id,
            "account_id": account.id,
            "provider_transaction_id": "archived-check",
            "amount": Decimal("-1.00"),
            "date": date(2026, 2, 1),
        },
    )
    transaction = await repo.get_transaction(session, transaction_id)
    with pytest.raises(ValidationError, match="archived"):
        await apply_category(
            session,
            transaction=transaction,
            category_id=created.json()["id"],
            actor="agent:categorizer-v1",
            confidence=0.99,
        )


async def test_duplicate_category_names_are_rejected(api_client, business):
    """Case-insensitive uniqueness, including for top-level categories."""
    first = await api_client.post(
        f"/businesses/{business.id}/categories",
        json={"name": "Travel", "account_type": "expense"},
    )
    assert first.status_code == 201

    duplicate = await api_client.post(
        f"/businesses/{business.id}/categories",
        json={"name": "travel", "account_type": "expense"},
    )
    assert duplicate.status_code >= 400
