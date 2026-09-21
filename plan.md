# Bookkeeping automation — project plan

## 1. Goal

Sync Coding Crafts' (and other owned businesses') bank transactions into Supabase Postgres automatically, categorize them via an agent as soon as they land, and let a human review/correct via CLI. Built for agents as the primary consumer; human interaction is CLI + MCP tools for now.

**Scope for v1:** single tenant, multiple businesses under that tenant (mirrors the existing Airtable Businesses table). Multi-tenant SaaS is on the roadmap but deferred — schema is shaped to support it without a rewrite (see §4).

## 2. Architecture overview

One core service, exposed through thin faces. Nothing agent-specific or CLI-specific lives outside the core.

```
      CLI (human)        Agents (LangGraph via MCP)
            \                     /
             \___________________/
                       |
              FastAPI core service
         (tx / rules / sync / categorize routes)
                       |
            Celery + Redis workers
     (sync_item, categorize_transaction tasks)
                       |
             Supabase Postgres
  (tenants, businesses, items, accounts, transactions,
              categorization_history)
```

Slack is deferred — not built in v1, but the "one core, N faces" shape means adding it later is additive (a new thin client calling the same routes), not a redesign. See §8.

Rules:
- Every face calls the *same* FastAPI routes. No duplicated business logic.
- Every write to a transaction's category goes through one function (`apply_category`), regardless of who called it, and appends to `categorization_history` rather than silently overwriting.
- Async work (sync, categorization) always goes through Celery — nothing long-running happens inline in a request handler.

## 3. Provider abstraction (Plaid swap-out requirement)

**Goal:** swapping Plaid for another aggregator later should mean writing one new adapter class and flipping an env var — not touching routes, Celery tasks, schema, or the categorization agent. Plaid is the only provider implemented in v1; the interface exists so that changes later.

### 3.1 The interface

Every provider implements this protocol. Nothing outside `providers/` imports a vendor SDK directly.

```python
from typing import Protocol, Optional
from dataclasses import dataclass

@dataclass
class LinkToken:
    token: str
    expiration: str

@dataclass
class ItemCredentials:
    provider_item_id: str
    access_token: str          # encrypted before storage, never logged
    institution_name: str

@dataclass
class RawTransaction:
    provider_transaction_id: str
    account_id: str
    amount: float
    date: str
    merchant_name: Optional[str]
    description: str
    pending: bool
    provider_category: Optional[str]
    raw_payload: dict          # untouched original — never discarded

@dataclass
class SyncResult:
    added: list[RawTransaction]
    modified: list[RawTransaction]
    removed: list[str]          # provider_transaction_ids
    next_cursor: str
    has_more: bool

@dataclass
class WebhookEvent:
    provider_item_id: str
    event_type: str              # normalized: "sync_available" | "item_error" | ...

class TransactionProvider(Protocol):
    def create_link_token(self, user_ref: str) -> LinkToken: ...
    def exchange_public_token(self, public_token: str) -> ItemCredentials: ...
    def sync_transactions(self, access_token: str, cursor: Optional[str]) -> SyncResult: ...
    def get_accounts(self, access_token: str) -> list[dict]: ...
    def verify_webhook(self, headers: dict, body: bytes) -> bool: ...
    def parse_webhook(self, body: dict) -> WebhookEvent: ...
    def force_refresh(self, access_token: str) -> None: ...
```

`PlaidProvider` implements this using the Plaid SDK. That's the *only* module in the codebase that imports `plaid`.

### 3.2 Provider registry

```python
def get_provider(name: str) -> TransactionProvider:
    return {
        "plaid": PlaidProvider(),
        # additional providers register here later, same interface
    }[name]
```

Selected via a `provider` column per item, not a global setting — so a future non-Plaid provider can be added per-account without touching existing items.

### 3.3 Testing proves the abstraction is real

Write a `FakeProvider` implementing the same protocol with canned data. Unit tests for routes/Celery tasks/categorization run against `FakeProvider`. Only a small integration test suite runs against real `PlaidProvider` + Plaid sandbox.

## 4. Data model

Two-level ownership: a **tenant** (the account holder — you, today; a future SaaS customer, eventually) owns multiple **businesses** (Coding Crafts, and any others). `tenant_id` is denormalized onto every table, not just `businesses` — this matches Supabase's standard multi-tenant RLS pattern (policies filter on `tenant_id` directly without needing a join) and costs nothing to carry now while single-tenant.

```sql
CREATE TABLE tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE businesses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    name text NOT NULL,
    details text,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE categories (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    business_id uuid NOT NULL REFERENCES businesses(id),
    name text NOT NULL,
    parent_category_id uuid REFERENCES categories(id),
    created_at timestamptz DEFAULT now(),
    UNIQUE (business_id, name, parent_category_id)
);

-- one row per provider connection (a Plaid item)
CREATE TABLE items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    business_id uuid NOT NULL REFERENCES businesses(id),
    provider text NOT NULL DEFAULT 'plaid',
    provider_item_id text,
    access_token_encrypted text,
    cursor text,
    institution_name text,
    backfill_start_date date,        -- boundary requested for the initial historical pull, see §5
    last_synced_at timestamptz,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    business_id uuid NOT NULL REFERENCES businesses(id),
    item_id uuid REFERENCES items(id),
    name text NOT NULL,
    provider_account_id text,
    account_type text,
    classification text CHECK (classification IN ('asset','liability')),
    opening_balance numeric,
    current_balance numeric,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE transactions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    business_id uuid NOT NULL REFERENCES businesses(id),
    account_id uuid NOT NULL REFERENCES accounts(id),
    category_id uuid REFERENCES categories(id),

    provider_transaction_id text,
    amount numeric NOT NULL,             -- signed: negative = money out
    date date NOT NULL,
    post_date date,
    vendor text,
    customer text,
    description text,
    memo text,
    transaction_type text,
    balance_after numeric,

    provider_category text,               -- raw suggestion from Plaid — informational only, never trusted
    confidence numeric,                    -- AI's confidence in the CURRENT category_id
    needs_review boolean NOT NULL DEFAULT false,   -- true when confidence is below threshold
    last_reviewed_at timestamptz,          -- when a human last confirmed/corrected this

    raw_payload jsonb,
    created_at timestamptz DEFAULT now(),
    UNIQUE (account_id, provider_transaction_id)
);

CREATE TABLE categorization_history (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    transaction_id uuid NOT NULL REFERENCES transactions(id),
    actor text NOT NULL,                   -- 'agent:categorizer-v1' | 'cli:hakeem'
    category_id uuid REFERENCES categories(id),
    confidence numeric,
    rationale text,
    created_at timestamptz DEFAULT now()
);
```

Notes:
- `needs_review` and `confidence` are distinct from `last_reviewed_at`: the first two are the agent's own signal at write time; the third only changes when a human actually looks at it via CLI. A transaction can have high confidence and still never have been human-reviewed — that's expected, not a bug.
- `apply_category()` is the one function that ever updates `category_id`, `confidence`, and `needs_review` on the transaction row, and always appends a row to `categorization_history`. Setting `last_reviewed_at` is a separate, explicit CLI action (`books tx recategorize` / `books tx confirm`), not something the categorization agent touches.

## 5. Sync flow

Hybrid push + pull, with a bounded initial backfill:

1. `books link` → Plaid Link flow → exchange token → store encrypted, `provider='plaid'`.
2. **Initial backfill, from a start date:** `books sync --backfill --since 2025-01-01`. Plaid's `/transactions/sync` has no date-range parameter — it's purely cursor-based and returns full available history from the item's inception. So the backfill loop still calls `/transactions/sync` from `cursor=None` until `has_more=False`, but the ingestion step discards any transaction dated before the requested `--since` date before writing to Postgres. The requested boundary is stored on `items.backfill_start_date` for reference. If `--since` is omitted, nothing is discarded and you get full available history (up to Plaid's normal 24-month limit).
3. Ongoing: Plaid webhook (`SYNC_UPDATES_AVAILABLE`) → `/webhooks/plaid` → ack fast → `sync_item.delay(item_id)`. Ongoing sync is never date-bounded — it always applies everything Plaid reports going forward.
4. Safety net: Celery Beat calls `sync_item` for every active item hourly regardless of webhook — cheap no-op via cursor when nothing's new.
5. Every newly added transaction from steps 2–4 enqueues `categorize_transaction.delay(tx_id)`.

## 6. Categorization

- LangGraph subgraph: retrieve similar past transactions + standing rules (scoped to the transaction's `business_id`) → call model → validate against that business's chart of accounts → write via `apply_category()` with a confidence score.
- Below a confidence threshold → `needs_review = true`.
- `apply_category()` is the single write path — called identically by the agent and by the CLI. Every call appends to `categorization_history`.

## 7. Interfaces (v1)

| Face | Tech | Talks to |
|---|---|---|
| CLI | Python (Typer), thin REST client | FastAPI core |
| Agents | MCP tools wrapping the same FastAPI routes | FastAPI core |

## 8. Deferred

- **Slack bot** — notifications + interactive corrections. Deferred, not dropped; adding it later is a new thin client on the existing core, not a redesign.
- **Non-Plaid providers** (e.g. a CSV-import path for banks Plaid doesn't cover) — the `TransactionProvider` interface supports this, but no second provider is being built now.
- **Multi-tenant auth/billing** — schema supports multiple businesses per tenant now; real per-tenant auth, RLS policies, and billing are SaaS-phase work, not v1.

## 9. Build milestones (v1)

1. Plaid Trial plan + use-case form (kick off day 1, external dependency)
2. Supabase project + schema migration (tenants, businesses, items, accounts, transactions, categorization_history)
3. Provider interface + `PlaidProvider` + `FakeProvider`
4. FastAPI core routes (tx, rules, sync, webhook receiver)
5. Celery + Redis: `sync_item`, `categorize_transaction`, Beat schedule
6. `books link` CLI flow (local callback server, token exchange)
7. Bounded initial backfill (`--since` handling)
8. LangGraph categorization subgraph + confidence/`needs_review` routing
9. MCP wrapper over core routes
10. CLI (Typer) — full command surface
11. Deploy, reconciliation cron, secrets hygiene, security pass

## 10. Open decisions

- [ ] Chart of accounts — final category list / structure per business
- [ ] Confidence threshold for `needs_review`
- [ ] Where FastAPI/Celery/Redis get hosted (existing infra vs. new)
- [ ] Whether `books-service` (prior WIP) gets reused or superseded — pending review of what's already built
- [ ] Retention/backup policy for `raw_payload` (grows unbounded; fine at this scale, revisit before multi-tenant)

## 11. Suggested repo structure

```
books/
├── providers/
│   ├── base.py          # TransactionProvider protocol
│   ├── plaid.py         # PlaidProvider — only file importing `plaid`
│   └── fake.py          # FakeProvider for tests
├── api/
│   ├── routes/          # tx, rules, sync, webhooks
│   └── main.py
├── workers/
│   ├── tasks.py          # sync_item, categorize_transaction
│   └── beat_schedule.py
├── agent/
│   └── categorizer.py    # LangGraph subgraph
├── cli/
│   └── main.py           # Typer app
├── mcp/
│   └── server.py         # MCP tool wrapper over api/routes
├── db/
│   └── migrations/
└── tests/
```
