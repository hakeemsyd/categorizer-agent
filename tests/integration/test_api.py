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


async def test_webhooks_do_not_require_the_bearer_token(api_client, linked_connection, dispatcher):
    """Providers authenticate by signature, not by our shared secret."""
    response = await api_client.post(
        f"/webhooks/{linked_connection.provider}",
        json={"connection_id": linked_connection.provider_ref, "event_type": "sync_available"},
        headers={"Authorization": ""},
    )
    assert response.status_code == 200
    assert response.json()["detail"] == "Sync queued"
    assert [name for name, _ in dispatcher.calls] == ["sync_connection"]


async def test_webhook_for_an_unknown_connection_is_acknowledged_not_errored(
    api_client, dispatcher
):
    response = await api_client.post("/webhooks/fake", json={"connection_id": "nope"})
    assert response.status_code == 200
    assert "Unknown connection" in response.json()["detail"]
    assert dispatcher.calls == []


async def test_connection_error_webhook_marks_the_connection(
    api_client, session, linked_connection
):
    response = await api_client.post(
        "/webhooks/fake",
        json={"connection_id": linked_connection.provider_ref, "event_type": "connection_error"},
    )
    assert response.status_code == 200
    # Same session, so the route's mutation is visible on the identity-mapped row.
    assert linked_connection.status == "error"


async def test_missing_row_maps_to_404(api_client):
    response = await api_client.get("/transactions/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"] == "NotFoundError"


async def test_sync_queues_by_default_and_runs_inline_on_wait(
    api_client, linked_connection, dispatcher
):
    queued = await api_client.post("/sync", json={"connection_id": str(linked_connection.id)})
    assert queued.status_code == 200
    assert [name for name, _ in dispatcher.calls] == ["sync_connection"]

    dispatcher.calls.clear()
    inline = await api_client.post(
        "/sync", json={"connection_id": str(linked_connection.id), "wait": True}
    )
    body = inline.json()["results"][0]
    assert body["inserted"] == 5
    # Every newly ingested transaction is queued for categorization (plan.md §5.5).
    assert [name for name, _ in dispatcher.calls] == ["categorize_transaction"] * 5


async def test_transaction_filters(api_client, linked_connection):
    await api_client.post("/sync", json={"connection_id": str(linked_connection.id), "wait": True})

    everything = await api_client.get(
        "/transactions", params={"business_id": str(linked_connection.business_id)}
    )
    assert everything.json()["total"] == 5

    uncategorized = await api_client.get(
        "/transactions",
        params={"business_id": str(linked_connection.business_id), "uncategorized": True},
    )
    assert uncategorized.json()["total"] == 5

    searched = await api_client.get(
        "/transactions",
        params={"business_id": str(linked_connection.business_id), "search": "gusto"},
    )
    assert searched.json()["total"] == 1
    assert searched.json()["connections"][0]["vendor"] == "Gusto"

    not_reviewed = await api_client.get(
        "/transactions",
        params={"business_id": str(linked_connection.business_id), "reviewed": False},
    )
    assert not_reviewed.json()["total"] == 5  # nothing has been reviewed yet

    reviewed = await api_client.get(
        "/transactions",
        params={"business_id": str(linked_connection.business_id), "reviewed": True},
    )
    assert reviewed.json()["total"] == 0


async def test_recategorize_by_name_marks_reviewed_and_records_the_actor(
    api_client, session, business, chart_of_accounts, linked_connection
):
    await api_client.post("/sync", json={"connection_id": str(linked_connection.id), "wait": True})
    page = await api_client.get(
        "/transactions", params={"business_id": str(business.id), "search": "aws"}
    )
    transaction_id = page.json()["connections"][0]["id"]

    response = await api_client.post(
        f"/transactions/{transaction_id}/recategorize",
        json={"category_name": "Software & Subscriptions", "actor": "cli:hakeem", "note": "infra"},
    )
    assert response.status_code == 200
    body = response.json()["transaction"]
    assert body["needs_review"] is False
    assert body["last_reviewed_at"] is not None

    history = await api_client.get(f"/transactions/{transaction_id}/history")
    assert history.json()[0]["actor"] == "cli:hakeem"
    assert history.json()[0]["rationale"] == "infra"


async def test_recategorize_to_an_unknown_category_is_rejected(
    api_client, business, chart_of_accounts, linked_connection
):
    await api_client.post("/sync", json={"connection_id": str(linked_connection.id), "wait": True})
    page = await api_client.get("/transactions", params={"business_id": str(business.id)})
    transaction_id = page.json()["connections"][0]["id"]

    response = await api_client.post(
        f"/transactions/{transaction_id}/recategorize",
        json={"category_name": "Nonexistent", "actor": "cli:hakeem"},
    )
    assert response.status_code == 422
    assert "chart of accounts" in response.json()["detail"]


async def test_categorize_refuses_an_already_reviewed_transaction_without_force(
    api_client, business, chart_of_accounts, linked_connection
):
    await api_client.post("/sync", json={"connection_id": str(linked_connection.id), "wait": True})
    page = await api_client.get("/transactions", params={"business_id": str(business.id)})
    transaction_id = next(t["id"] for t in page.json()["connections"] if t["vendor"] == "AWS")
    await api_client.post(
        f"/transactions/{transaction_id}/recategorize",
        json={"category_name": "Payroll", "actor": "cli:hakeem"},
    )
    # A standing rule so `categorize` short-circuits before the model —
    # this test is about the review guard, not the classifier.
    await api_client.post(
        "/rules",
        json={
            "business_id": str(business.id),
            "match_type": "vendor_equals",
            "pattern": "AWS",
            "category_name": "Software & Subscriptions",
        },
    )

    refused = await api_client.post(
        f"/transactions/{transaction_id}/categorize", json={"wait": True}
    )
    assert refused.status_code == 422
    assert "reviewed by a human" in refused.json()["detail"]

    forced = await api_client.post(
        f"/transactions/{transaction_id}/categorize", json={"wait": True, "force": True}
    )
    assert forced.status_code == 200
    assert forced.json()["category_name"] == "Software & Subscriptions"


async def test_categorize_batch_skips_reviewed_rows_by_default(
    api_client, business, chart_of_accounts, linked_connection, dispatcher
):
    await api_client.post("/sync", json={"connection_id": str(linked_connection.id), "wait": True})
    page = await api_client.get("/transactions", params={"business_id": str(business.id)})
    transactions = page.json()["connections"]
    assert len(transactions) == 5

    # Review one of them; it must be excluded from the default batch scope.
    await api_client.post(
        f"/transactions/{transactions[0]['id']}/recategorize",
        json={"category_name": "Payroll", "actor": "cli:hakeem"},
    )
    dispatcher.calls.clear()

    response = await api_client.post(
        "/transactions/categorize-batch", json={"business_id": str(business.id)}
    )
    assert response.status_code == 200
    assert response.json()["queued"] == 4

    queued_ids = {
        str(call[0]) for name, call in dispatcher.calls if name == "categorize_transaction"
    }
    assert transactions[0]["id"] not in queued_ids
    # Skipped by the query, so force is never even needed for the default case.
    assert all(force is False for _, (_, force) in dispatcher.calls)


async def test_categorize_batch_include_reviewed_forces_every_matched_row(
    api_client, business, chart_of_accounts, linked_connection, dispatcher
):
    await api_client.post("/sync", json={"connection_id": str(linked_connection.id), "wait": True})
    page = await api_client.get("/transactions", params={"business_id": str(business.id)})
    transactions = page.json()["connections"]

    await api_client.post(
        f"/transactions/{transactions[0]['id']}/recategorize",
        json={"category_name": "Payroll", "actor": "cli:hakeem"},
    )
    dispatcher.calls.clear()

    response = await api_client.post(
        "/transactions/categorize-batch",
        json={"business_id": str(business.id), "include_reviewed": True},
    )
    assert response.json()["queued"] == 5
    assert all(force is True for _, (_, force) in dispatcher.calls)


async def test_categorize_batch_filters_by_category_name(
    api_client, business, chart_of_accounts, linked_connection, dispatcher
):
    await api_client.post("/sync", json={"connection_id": str(linked_connection.id), "wait": True})
    page = await api_client.get("/transactions", params={"business_id": str(business.id)})
    transaction_id = next(t["id"] for t in page.json()["connections"] if t["vendor"] == "AWS")
    await api_client.post(
        f"/transactions/{transaction_id}/recategorize",
        json={"category_name": "Software & Subscriptions", "actor": "cli:hakeem"},
    )
    dispatcher.calls.clear()

    response = await api_client.post(
        "/transactions/categorize-batch",
        json={
            "business_id": str(business.id),
            "category_name": "Software & Subscriptions",
            "include_reviewed": True,
        },
    )
    assert response.json()["queued"] == 1


async def test_categorize_batch_rejects_an_unknown_category_name(
    api_client, business, chart_of_accounts, linked_connection
):
    response = await api_client.post(
        "/transactions/categorize-batch",
        json={"business_id": str(business.id), "category_name": "Nonexistent"},
    )
    assert response.status_code == 422


async def test_categorize_batch_404s_for_an_unknown_business(api_client):
    response = await api_client.post(
        "/transactions/categorize-batch",
        json={"business_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 404


async def test_categorize_batch_with_nothing_matching_queues_nothing(api_client, business):
    response = await api_client.post(
        "/transactions/categorize-batch", json={"business_id": str(business.id)}
    )
    assert response.status_code == 200
    assert response.json()["queued"] == 0


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
    api_client, business, session, linked_connection
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


async def test_chart_bootstrap_applies_exactly_the_approved_proposal(api_client, business):
    """Approval is for a specific list, so the route creates that list.

    The model is not consulted on this path at all — which is also why this
    test can exercise the real endpoint.
    """
    response = await api_client.post(
        f"/businesses/{business.id}/categories/bootstrap",
        json={
            "apply": True,
            "proposed": [
                {
                    "name": "Cloud Hosting",
                    "account_type": "expense",
                    "description": "Servers and managed infrastructure",
                    "covers": ["AWS"],
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert [c["name"] for c in body["created"]] == ["Cloud Hosting"]
    assert body["applied"] is True

    listing = await api_client.get(f"/businesses/{business.id}/categories")
    assert [c["name"] for c in listing.json()] == ["Cloud Hosting"]


async def test_chart_bootstrap_will_not_take_a_proposal_it_is_not_allowed_to_apply(
    api_client, business
):
    response = await api_client.post(
        f"/businesses/{business.id}/categories/bootstrap",
        json={
            "apply": False,
            "proposed": [{"name": "X", "account_type": "expense", "description": "x"}],
        },
    )
    assert response.status_code == 422
