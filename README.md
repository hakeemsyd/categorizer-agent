# Books — bookkeeping automation

Pulls bank transactions into Postgres, categorizes them against a real chart of
accounts with an agent as soon as they land, and gives you a CLI to review and
correct the ones it was unsure about. Built for multiple businesses under one
owner.

Full design rationale lives in [`plan.md`](plan.md). This file is how to run it.

---

## How it fits together

```
      CLI (you)          Agents (MCP tools)
            \                   /
             \_________________/
                      |
             FastAPI core service          ← all business logic
                      |
            Celery + Redis workers         ← sync + categorize, off the request path
                      |
              Postgres (Supabase)
```

Three rules hold the shape:

1. **Every face calls the same routes.** The CLI and the MCP server both go
   through `books.sdk.BooksClient`. Adding Slack, or a web UI, is a new
   directory under `src/books/faces/` — not a second copy of the logic.
2. **One category write path.** `books.core.categorization.apply_category` is
   the only function that touches `transactions.category_id`, whoever called
   it, and it always appends to `categorization_history`.
3. **One vendor import.** Only `src/books/providers/teller.py` talks to
   Teller's API. Everything else talks to the `TransactionProvider` protocol,
   and the test suite runs against `FakeProvider` to keep that honest.

---

## Running it

There are two supported ways to run this, and the difference is deliberate.

| | **Development** | **Production** |
|---|---|---|
| Core service, worker, beat | native, in `.venv` | containers from `Dockerfile` |
| Postgres | container (`make up`) or local install | **Supabase** — managed, never containerized |
| Redis | container (`make up`) | managed (Upstash, ElastiCache, Redis Cloud) |
| Migrations | `make migrate` by hand | `alembic upgrade head` on deploy |
| Edit/restart loop | under a second | n/a |

A third mode exists for convenience: **the whole stack in containers**
(`make stack`). Use it for a clean checkout, a demo, or to smoke-test the image
you are about to deploy — not for daily work, where the native loop is much
faster.

---

## Development

Requirements: Python 3.12+, Docker (or your own Postgres 14+ and Redis).

```bash
make install            # creates .venv and installs the project
cp .env.example .env    # then fill it in — see "Configuration"
make up                 # Postgres + Redis in containers; nothing else
make migrate            # create the schema
```

Then three terminals:

```bash
make api            # the core service, with reload
make worker         # sync + categorization tasks
make beat           # the hourly sync safety net
```

`make up` starts **only** Postgres and Redis. The app stays on your machine so
edits take effect immediately and the debugger works normally.

### Try it with no bank and no API keys

```bash
BOOKS_DEFAULT_PROVIDER=fake .venv/bin/python scripts/seed_demo.py
make api                                        # terminal 1
books tx list --business "Coding Crafts"        # terminal 2
```

`FakeProvider` supplies canned accounts and transactions, so you can exercise
the CLI, the review loop and the MCP tools before Teller access arrives.

### The real setup

```bash
books init --tenant "Your Name" --business "Coding Crafts"   # seeds the chart
books category list -b "Coding Crafts"                       # review and adjust
books link -b "Coding Crafts" --since 2025-01-01
books sync --backfill --since 2025-01-01
books tx review -b "Coding Crafts"
```

### The whole stack in containers

```bash
make stack          # builds the image, migrates, starts api + worker + beat
make stack-logs
make stack-down     # stops them; the database volume survives
```

This uses the `app` compose profile. `migrate` runs first and the other
services wait for it to succeed, so a clean checkout comes up migrated in one
command. The CLI still runs on your machine and talks to the container:

```bash
BOOKS_API_BASE_URL=http://localhost:8000 books health
```

---

## Production

The image is one artifact with three roles — the API, the Celery worker, and
Beat differ only by their command:

```bash
docker build -t books:$(git rev-parse --short HEAD) .

# API
docker run --env-file .env.production -p 8000:8000 books:TAG

# Worker
docker run --env-file .env.production books:TAG \
  celery -A books.workers.app.celery_app worker -Q books -l info

# Beat — exactly one replica, ever
docker run --env-file .env.production books:TAG \
  celery -A books.workers.app.celery_app beat -l info \
  --schedule /tmp/celerybeat-schedule
```

Deploy checklist:

- **Database is Supabase**, not a container. Point `BOOKS_DATABASE_URL` at it
  (session pooler connection string, `+asyncpg`). The compose Postgres exists
  for development only.
- **Redis is managed.** Celery needs it as broker and result backend.
- **Run `alembic upgrade head` before rolling the new image**, as a release
  step or an init container. The `migrate` compose service shows the shape.
- **Scale the API and worker freely; run exactly one Beat.** Two Beat
  processes means every item syncs twice an hour.
- **`BOOKS_ENV=production`** switches logging from console output to JSON.
- **Secrets come from your platform's secret store**, not a baked `.env`.
  `BOOKS_ENCRYPTION_KEY` in particular: lose it and every stored access token
  becomes undecryptable, and every bank has to be re-linked. Back it up
  somewhere you would trust with a password manager's master key.
- **Register `/webhooks/teller` (publicly reachable HTTPS) in the Teller
  Dashboard.** Teller signs each delivery with `BOOKS_TELLER_SIGNING_SECRET`;
  the route verifies that signature rather than your bearer token.
- **Outside sandbox, mount a real certificate and key** and point
  `BOOKS_TELLER_CERT_PATH` / `BOOKS_TELLER_KEY_PATH` at them — Teller
  authenticates every API call with mutual TLS, not just an API key.
- **The container runs as non-root** (uid 1001) and holds no state — the only
  writable path it needs is `/tmp`, for Beat's schedule file.

---

## Configuration

Everything is read from `.env` with a `BOOKS_` prefix
(see [`.env.example`](.env.example) and `src/books/config.py`). The ones that
matter:

| Setting | What it does |
|---|---|
| `BOOKS_DATABASE_URL` | Supabase Postgres, with `+asyncpg`. |
| `BOOKS_REDIS_URL` | Celery broker and result backend. |
| `BOOKS_API_TOKEN` | Shared bearer token every face presents to the core. |
| `BOOKS_ENCRYPTION_KEY` | Fernet key encrypting provider access tokens at rest. |
| `BOOKS_TELLER_APPLICATION_ID` | Public — Teller Connect embeds it client-side. |
| `BOOKS_TELLER_ENVIRONMENT` | `sandbox` (default), `development`, or `production`. |
| `BOOKS_TELLER_CERT_PATH` / `_KEY_PATH` | Mutual-TLS cert/key. Required outside sandbox. |
| `BOOKS_TELLER_SIGNING_SECRET` | Verifies `Teller-Signature` on incoming webhooks. |
| `BOOKS_ANTHROPIC_API_KEY` | Powers the categorization agent. |
| `BOOKS_CATEGORIZER_MODEL` | Defaults to `claude-sonnet-5`. |
| `BOOKS_CONFIDENCE_THRESHOLD` | Below this, a transaction is flagged for review. |
| `BOOKS_ANTHROPIC_WORKSPACE_ID` | Only for an org-level key that must name a workspace. |
| `BOOKS_DEFAULT_PROVIDER` | `teller`, or `fake` for demos and tests. |

Two keys never leave the core service: `BOOKS_ENCRYPTION_KEY` and
`BOOKS_TELLER_SIGNING_SECRET`. Provider access tokens are encrypted before
they reach Postgres, and redacted from every log line — as is the mutual-TLS
private key, which never gets logged at all since only its *path* is ever
passed around as configuration.

Running natively, settings are read from `.env` in the project root. Running
under compose, the same `.env` is picked up for interpolation and passed
through to the containers — except `BOOKS_DATABASE_URL` and `BOOKS_REDIS_URL`,
which compose overrides to the `postgres` and `redis` service names. In
production, supply everything from your platform's secret store.

---

## The accounting model

Every category is a line in a chart of accounts with one of the five
fundamental **account types**, and every transaction carries the **entry side**
it applies to that category.

| Account type | Increases on | What it is |
|---|---|---|
| `asset` | debit | What the business owns or is owed |
| `liability` | credit | What the business owes |
| `equity` | credit | The owner's stake — contributions and draws |
| `revenue` | credit | Income earned from operations |
| `expense` | debit | Costs incurred to operate |

`books category types` prints this table.

**Entry side.** Amounts are signed from the bank's point of view: negative means
money left the account. So money out **debits** the category you assign and
money in **credits** it; the bank account takes the opposite side, which is the
other half of the entry.

`transactions.entry_side` is a Postgres **generated column** derived from the
sign of the amount. It cannot be set by hand — the database rejects the write —
so it can never disagree with the amount it describes. (A zero-amount
transaction is recorded as a credit; it moves no balance either way.)

**Money out is not always an expense**, and the agent is told so explicitly:
repaying loan principal debits a liability, an owner taking money out debits
equity, and moving cash between your own accounts debits an asset. Likewise
money in is not always revenue — a refund **credits the expense it came from**,
which reduces that expense rather than inflating income.

That last point is why `entry_side` matters rather than just the amount's sign:
`increases_balance()` in `books.core.accounting` is what separates a 412.55 AWS
charge from a 412.55 AWS refund when you total up the expense account.

### Getting a chart of accounts

The agent can only file a transaction into an account that exists, so the
buckets come first. There are two ways to get them.

**From your own transactions** — better, once you have synced anything:

```bash
books sync -b "Coding Crafts"
books category bootstrap -b "Coding Crafts"   # proposes; you approve
```

It reads every merchant in the business's transactions and proposes the
accounts needed to cover them, with the merchants each one is for so you can
check the reasoning. Nothing is written until you say yes, and what you
approved is exactly what gets created — the confirmed list is sent back rather
than the model being asked a second time. Accounts you already have are never
renamed, replaced or removed, so it is safe to re-run after a later sync to
cover merchants that have since appeared.

**Generic** — when there is nothing synced yet:

```bash
books category seed -b "Coding Crafts"      # ~40 standard accounts
books category list -b "Coding Crafts"
books category list -b "Coding Crafts" -t revenue
```

Seeding is idempotent: accounts you already have are left exactly as they are,
so you can run it again after adding your own. `books init --business NAME`
seeds automatically; pass `--no-seed` to skip.

Either way, `books tx bootstrap` refuses to run against an empty chart and
tells you which of these to reach for.

To bring your own, `books category import` takes a CSV with `name`,
`account_type` and an optional `description` — see
[`chart_of_accounts.example.csv`](chart_of_accounts.example.csv). Descriptions
are not decoration: they go into the categorization prompt, so write them to
tell the agent where the boundaries are.

Sub-accounts are supported via `parent_category_id` and must share their
parent's account type.

---

## The CLI

```
books health                                  Is the core service up?
books init --tenant NAME [--business NAME]    First-run setup

books business list | add NAME
books category list [-t TYPE] | seed | add NAME -t TYPE
books category bootstrap -b BUSINESS           Draft a chart from synced merchants
books category import FILE.csv | archive ID | types
books item list | accounts | refresh ID

books link -b BUSINESS [--since YYYY-MM-DD]   Connect a bank in the browser
books sync [-b BUSINESS] [--backfill] [--since DATE] [--queue]

books tx bootstrap -b BUSINESS                First pass: one decision per merchant
books tx list -b BUSINESS [--needs-review] [--uncategorized] [--reviewed] [--search TEXT]
books tx show ID                              Detail + full audit trail
books tx categorize ID                        Run the agent over one row
books tx recategorize ID -c "Category"        Correct it (marks reviewed, propagates)
books tx confirm ID                           Agree with it (marks reviewed)
books tx review -b BUSINESS                   Walk the review queue

books rule list | add PATTERN -c CATEGORY | rm ID
```

`books tx review` is the main loop: Enter confirms, typing a category name
corrects, `r` also writes a standing rule for that vendor so the same
correction never comes back, `s` skips, `q` quits.

### Connecting a bank

`books link` asks the core for a link token (Teller's public
`application_id`), serves Teller Connect on `127.0.0.1:8420`, and posts
whatever comes back to the core for exchange. Unlike some aggregators, Teller
Connect's `onSuccess` hands the browser the real, finished access token
directly — there's no separate exchange step — but the core still confirms
it server-side (by fetching the enrollment's accounts) rather than trusting
the browser's claims, and the token itself never touches your shell history.

```bash
books link -b "Coding Crafts"
```

**Sandbox** (the default, `BOOKS_TELLER_ENVIRONMENT=sandbox`) needs only
`BOOKS_TELLER_APPLICATION_ID` — no certificate. Pick any institution and log
in with username `username` / password `password`; other usernames simulate
MFA challenges, account lockouts, and disconnections (see Teller's
[sandbox guide](https://teller.io/docs/guides/sandbox)).

**Development/production** additionally need `BOOKS_TELLER_CERT_PATH` /
`BOOKS_TELLER_KEY_PATH` — Teller authenticates every API call with mutual
TLS, not just an API key. `books link` still works the same way; only the
server-side calls behind it now present the certificate.

Register `https://<your-tunnel>/webhooks/teller` in the Teller Dashboard for
the fast sync path (`ngrok http 8000` in dev, matching
`BOOKS_TELLER_SIGNING_SECRET` for verification). Without it, Celery Beat's
hourly pass still picks up new transactions — you just wait longer.

### Backfill and `--since`

Teller has no delta-sync endpoint — transactions are paginated per account
with `count`/`from_id`/date filters, and there is no signal for "what
changed since I last looked." So `--backfill --since 2025-01-01` walks full
history per account and discards anything older at ingestion, storing the
boundary on the item for reference. Ongoing (non-backfill) syncs re-walk a
trailing window on every incremental pass — see `TELLER_SYNC_OVERLAP_DAYS` in
`providers/teller.py` — rather than trusting a single watermark, since a
`pending` transaction can later post under a different id with no signal
that it happened. Duplicates from the overlap are harmless: ingestion upserts
by id and never touches a category a human has already set.

---

## Categorization

For each new transaction the agent (a LangGraph subgraph in
`src/books/agent/categorizer.py`):

1. loads the business's chart of accounts (with each account's type), its
   standing rules, and past transactions that look similar;
2. if a rule matches, uses it and **skips the model entirely**;
3. otherwise asks the model for a category, confidence, and rationale — the
   prompt states whether this transaction debits or credits the category, and
   spells out the cases where money out is not an expense;
4. rejects anything outside the chart of accounts rather than inventing a
   category;
5. writes through `apply_category`, which flags the row for review when
   confidence is below `BOOKS_CONFIDENCE_THRESHOLD`.

`needs_review` and `confidence` are the agent's own signals. `last_reviewed_at`
moves only when a human confirms or corrects — a high-confidence transaction
that no one has looked at is normal, not a bug.

### The merchant is the unit of decision

Transactions repeat by merchant: in the sample data, 222 transactions come
from 95 merchants, and that ratio only improves as history grows. So the
system groups on a **normalized merchant key** (`vendor_key`, from
`books.core.vendors`) — `SQ *BLUE BOTTLE #417` and `BLUE BOTTLE` are one
merchant, while `UBER` and `UBER EATS` deliberately stay apart. Payment rails
("Incoming Wire", "Zelle Payment") are not merchants and group on nothing.

But the merchant alone is too coarse to *copy an answer across*, because one
merchant routinely covers more than one account:

| Same merchant | | Different accounts |
|---|---|---|
| `AMERICAN EXPRESS \| Platinum Card` | both money out | Credit Card Payable |
| `AMERICAN EXPRESS \| Interest Payment` | | Interest Expense |
| `SAM BLOCK \| Zelle Payment` −117.65 | opposite directions | contractor spend |
| `SAM BLOCK \| Zelle Payment` +43.40 | | not the same event |

So a decision generalizes only within an identical **`decision_key`** —
merchant, direction, *and* what the line says. A description that merely
restates the merchant (`SHELL | Shell`) discriminates nothing and is ignored,
so normal merchants still group as one. Splitting too finely costs one extra
human decision; merging too coarsely silently misbooks money.

That one idea is what makes all three parts of the quality loop work.

**Cold start — nothing to learn from yet.** Two steps, in this order: the
buckets, then what goes in them.

```bash
books category bootstrap -b "Coding Crafts"  # step 1: the chart, from your merchants
books tx bootstrap -b "Coding Crafts"        # step 2: add --max-vendors 5 to try it small
```

Step one groups the same way step two does, so the chart is proposed against
the decisions that will actually be made — no account is proposed for a
merchant that will never be asked about, and no merchant is left without
somewhere to go.

One model call per merchant rather than per transaction, applied to that
merchant's whole group. On the sample data that's 93 calls instead of 220, and
the same shop can't land in two categories. Nothing is marked reviewed — it's
still the model's opinion, just organized the way a human wants to review it.

**Getting better — verified work becomes the evidence.** The prompt ranks what
it's shown, strongest first: a *correction* (a human overruling the agent for
this merchant), then a human-reviewed transaction, then the agent's own earlier
guesses (explicitly labelled as such, so agreement with itself isn't mistaken
for confirmation), then the provider's hint.

**Adapting — one correction fixes the rest.**

```bash
books tx recategorize <id> -c "Subcontractors & Freelancers"
# ✓ Set to Subcontractors & Freelancers and marked reviewed
#   Also updated 4 other SAM BLOCK transaction(s).
```

A correction reaches the other **unreviewed** transactions with the same
`decision_key` — same merchant, same direction, same kind of line. Rows another human
already ruled on are never touched — `apply_category` refuses that write
regardless. Propagated rows are recorded as `propagation:cli:you`, not as
though you reviewed them personally, and `--only-this-one` opts out.

Rules are the next step up, for when you want the model skipped entirely:

```bash
books rule add "GUSTO" -c "Payroll" -b "Coding Crafts"
```

---

## Agent access (MCP)

```bash
make mcp    # or: books-mcp
```

Register it with any MCP client:

```json
{
  "mcpServers": {
    "books": {
      "command": "/absolute/path/to/.venv/bin/books-mcp",
      "env": {
        "BOOKS_API_BASE_URL": "http://localhost:8000",
        "BOOKS_API_TOKEN": "..."
      }
    }
  }
}
```

The tools are the same routes the CLI uses. `recategorize_transaction` and
`confirm_transaction` take an `actor` and mark a transaction human-reviewed, so
an agent should only call them on a person's behalf.

---

## Layout

```
src/books/
├── core/          All business logic. Nothing here imports FastAPI, Celery or a vendor SDK.
│   ├── accounting.py      Account types, normal balances, entry sides
│   ├── vendors.py         Merchant normalization — the grouping key
│   ├── chart_of_accounts.py  The default chart used by `books category seed`
│   ├── models.py          Schema (source of truth for migrations)
│   ├── repository.py      Data access
│   ├── categorization.py  apply_category — the single category write path
│   ├── ingest.py          RawTransaction -> rows, with the backfill boundary
│   ├── sync.py            Provider-agnostic sync orchestration
│   └── queue.py           Indirection over Celery (swappable in tests)
├── providers/     TransactionProvider protocol, TellerProvider, FakeProvider
├── agent/         LangGraph categorization subgraph, chart builder, cold-start bootstrap
├── workers/       Celery app, tasks, beat schedule, shared event loop
├── sdk/           BooksClient — the REST client every non-API face uses
├── faces/         Thin transports: api/, cli/, mcp/  (see faces/README.md)
└── security/      Token encryption
db/migrations/     Alembic
tests/             unit/ needs nothing; integration/ needs Postgres
scripts/           seed_demo.py

Dockerfile         One image, three roles (api / worker / beat)
docker-compose.yml Default: Postgres + Redis. `--profile app`: the whole stack
Makefile           `make help` lists every target
```

---

## Working on the code

```bash
make test     # pytest
make lint     # ruff + mypy
make fmt      # ruff format + autofix
```

Integration tests need a Postgres. They TRUNCATE every table, so they run
against a dedicated `books_test` database rather than your development one:

```bash
make test-db     # creates books_test beside whatever BOOKS_DATABASE_URL points at
make test
```

The test database is resolved from `BOOKS_DATABASE_URL` (env var, then `.env`)
with the database name swapped — so it follows your dev Postgres, compose or
otherwise. It refuses to derive anything from a non-local host; against a
remote server name it explicitly with `BOOKS_TEST_DATABASE_URL`.

If it is unreachable the database-backed tests **skip** with a message saying
so. A skipped run is not a passing run — check the summary line.

Tests never touch Teller or Anthropic: the provider is mocked at the HTTP
transport level (`tests/unit/test_teller_provider.py`) and the classifier is
injected.

Schema changes:

```bash
# edit src/books/core/models.py, then:
make revision m="add whatever"
make migrate
```

Starting over:

```bash
make reset-db     # drops everything, then rebuilds from migrations
```

This destroys all data, including linked banks — the stored access tokens go
with it, so re-link through `books link` afterwards. It refuses to run against
anything but a local host, so it cannot be pointed at Supabase. Stop the worker
and beat first; beat syncs hourly and will happily write into a database you
are in the middle of dropping.

### Adding a provider

1. Write `src/books/providers/<name>.py` implementing `TransactionProvider`.
2. Register it in `providers/registry.py`.
3. Nothing else changes — `items.provider` is per-row, so the new adapter works
   alongside Teller rather than replacing it.

`tests/unit/test_providers.py` checks every registered provider against the
protocol's method signatures, so an adapter that drifts fails the suite.

### Adding a frontend

See [`src/books/faces/README.md`](src/books/faces/README.md). Short version:
new directory under `faces/`, talk to `books.sdk.BooksClient`, add missing
endpoints to the API and the SDK rather than reaching into `books.core`.

---

## Operational notes

- **Webhooks** authenticate by Teller's `Teller-Signature` HMAC (verified
  against the raw body), not by `BOOKS_API_TOKEN` — `/webhooks/{provider}` is
  deliberately outside the bearer-token dependency.
- **Celery Beat** re-syncs every active item hourly as a safety net for missed
  webhooks. Teller has no "nothing changed" signal the way a cursor does, so
  each pass genuinely re-walks the trailing overlap window rather than being
  a true no-op — cheap, but not free, at real scale.
- **Workers use a threads pool** and one shared event loop per process
  (`workers/runner.py`); the work is IO-bound, and forking breaks on macOS.
- **Re-syncs never clobber a human.** Transaction upserts only overwrite
  provider-owned columns, so a category you set survives.
- **Categories are archived, never deleted** — history rows still point at them.
- **`entry_side` is generated by Postgres**, not by application code, so no
  face, task or migration can write a side that contradicts the amount.
- **The image carries no state.** Migrations are a separate step, Beat's
  schedule file lives in `/tmp`, and nothing is written to the filesystem
  otherwise — so replicas are interchangeable and restarts lose nothing.

## Not built yet

Slack bot, a second provider, and real per-tenant auth/RLS/billing. The schema
already carries `tenant_id` on every table for that last one. See `plan.md` §8.
