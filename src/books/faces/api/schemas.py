"""Wire shapes for the core API. Kept separate from the ORM on purpose."""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from books.core.accounting import AccountType, EntrySide


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- tenants / businesses ----------------------------------------------


class TenantCreate(BaseModel):
    name: str


class TenantOut(ORMModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class BusinessCreate(BaseModel):
    name: str
    details: str | None = None
    tenant_id: uuid.UUID | None = None


class BusinessOut(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    details: str | None
    created_at: datetime


# --- categories ---------------------------------------------------------


class CategoryCreate(BaseModel):
    name: str
    account_type: AccountType
    description: str | None = None
    parent_category_id: uuid.UUID | None = None


class CategoryOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    name: str
    account_type: AccountType
    #: The side that increases this account — derived from account_type.
    normal_balance: EntrySide
    description: str | None
    parent_category_id: uuid.UUID | None
    archived: bool


class SeedChartResponse(BaseModel):
    created: list[CategoryOut]
    skipped: list[str] = Field(
        default_factory=list, description="Names that already existed and were left alone"
    )


# --- items / accounts ---------------------------------------------------


class LinkTokenRequest(BaseModel):
    business_id: uuid.UUID
    provider: str | None = None


class LinkTokenOut(BaseModel):
    link_token: str
    expiration: str
    provider: str
    #: Which environment the widget should point at (e.g. Teller's
    #: sandbox/development/production). Empty for a provider with no such
    #: concept.
    environment: str = ""


class LinkExchange(BaseModel):
    business_id: uuid.UUID
    public_token: str
    provider: str | None = None
    backfill_start_date: date | None = None


class ItemOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    provider: str
    institution_name: str | None
    status: str
    backfill_start_date: date | None
    last_synced_at: datetime | None
    last_error: str | None
    created_at: datetime


class AccountOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    item_id: uuid.UUID | None
    name: str
    account_type: str | None
    #: Whether the bank account holds money (asset) or owes it (liability).
    classification: AccountType | None
    current_balance: Decimal | None


# --- sync ---------------------------------------------------------------


class SyncRequest(BaseModel):
    item_id: uuid.UUID | None = None
    business_id: uuid.UUID | None = None
    backfill: bool = False
    since: date | None = None
    wait: bool = Field(
        default=False,
        description="Run inline instead of queueing. Intended for CLI/dev use.",
    )


class SyncSummaryOut(BaseModel):
    item_id: uuid.UUID
    queued_task_id: str | None = None
    pages: int = 0
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    skipped_before_backfill: int = 0
    new_transaction_ids: list[uuid.UUID] = Field(default_factory=list)


class SyncResponse(BaseModel):
    results: list[SyncSummaryOut]


# --- transactions -------------------------------------------------------


class TransactionOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    account_id: uuid.UUID
    category_id: uuid.UUID | None
    date: dt.date
    post_date: dt.date | None
    amount: Decimal
    vendor: str | None
    customer: str | None
    description: str | None
    memo: str | None
    transaction_type: str | None
    #: Which side this transaction applies to its category. The bank account
    #: takes the opposite side. Generated from the sign of the amount.
    entry_side: EntrySide
    pending: bool
    provider_category: str | None
    confidence: Decimal | None
    needs_review: bool
    last_reviewed_at: datetime | None


class TransactionPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TransactionOut]


class RecategorizeRequest(BaseModel):
    """A human correction. Sets last_reviewed_at; the agent never does."""

    category_id: uuid.UUID | None = None
    category_name: str | None = None
    actor: str
    note: str | None = None


class ConfirmRequest(BaseModel):
    actor: str
    note: str | None = None


class CategorizeRequest(BaseModel):
    wait: bool = False
    #: Override the refusal to overwrite an already-reviewed transaction.
    force: bool = False


class CategorizeResponse(BaseModel):
    transaction_id: uuid.UUID
    queued_task_id: str | None = None
    category_id: uuid.UUID | None = None
    category_name: str | None = None
    account_type: AccountType | None = None
    confidence: float | None = None
    needs_review: bool | None = None
    rationale: str | None = None
    source: str | None = None


class CategorizeBatchRequest(BaseModel):
    """Queue the agent to run again over transactions matching these filters.

    Reuses the same filter shape as listing transactions. Already-reviewed
    transactions are skipped unless ``include_reviewed`` is set — see
    ``apply_category``'s ``force`` guard, which this maps onto.
    """

    business_id: uuid.UUID
    account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    category_name: str | None = None
    needs_review: bool | None = None
    uncategorized: bool | None = None
    start_date: date | None = None
    end_date: date | None = None
    include_reviewed: bool = False


class CategorizeBatchResponse(BaseModel):
    queued: int


class HistoryOut(ORMModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    actor: str
    category_id: uuid.UUID | None
    confidence: Decimal | None
    rationale: str | None
    created_at: datetime


# --- rules --------------------------------------------------------------


class RuleCreate(BaseModel):
    business_id: uuid.UUID
    match_type: str = Field(pattern="^(vendor_equals|vendor_contains|description_contains)$")
    pattern: str
    category_id: uuid.UUID | None = None
    category_name: str | None = None
    note: str | None = None
    priority: int = 100
    created_by: str | None = None


class RuleOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    match_type: str
    pattern: str
    category_id: uuid.UUID
    note: str | None
    priority: int
    active: bool
    created_at: datetime


# --- misc ---------------------------------------------------------------


class HealthOut(BaseModel):
    status: str
    version: str
    env: str
    database: str
    providers: list[str]


class Acknowledgement(BaseModel):
    ok: bool = True
    detail: str | None = None
