.DEFAULT_GOAL := help
VENV := .venv
PY := $(VENV)/bin/python
UV := uv

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install the project with dev extras
	$(UV) venv $(VENV)
	$(UV) pip install --python $(PY) -e ".[dev]"

up: ## Start local Postgres + Redis (app runs natively)
	docker compose up -d

down: ## Stop everything started by compose
	docker compose --profile app down

build: ## Build the application image
	docker compose --profile app build

stack: ## Run the whole stack in containers (migrates, then api + worker + beat)
	docker compose --profile app up -d --build
	@echo "API on http://localhost:8000 — logs: make stack-logs"

stack-logs: ## Tail the containerized app services
	docker compose --profile app logs -f api worker beat

stack-down: ## Stop the containerized stack, keeping the database volume
	docker compose --profile app down

migrate: ## Apply migrations to BOOKS_DATABASE_URL
	$(VENV)/bin/alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add x"
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

api: ## Run the core service with reload
	$(VENV)/bin/uvicorn books.faces.api.main:app --reload --port 8000

worker: ## Run a Celery worker
	$(VENV)/bin/celery -A books.workers.app.celery_app worker -Q books -l info

beat: ## Run Celery Beat (the hourly sync safety net)
	$(VENV)/bin/celery -A books.workers.app.celery_app beat -l info

flower: ## Web dashboard over the queue — http://localhost:5555 (debugging only)
	$(VENV)/bin/celery -A books.workers.app.celery_app flower --port=5555 --address=127.0.0.1

mcp: ## Run the MCP server over stdio
	$(PY) -m books.faces.mcp.server

reset-db: ## DESTROY the dev database and rebuild it from migrations
	$(PY) scripts/reset_db.py --yes
	$(VENV)/bin/alembic upgrade head
	@echo "Schema rebuilt. Re-link your bank: books link -b BUSINESS"

test-db: ## Create the test database beside the configured dev database
	$(PY) scripts/create_test_db.py

test: ## Run the test suite
	$(VENV)/bin/pytest -q

lint: ## Lint and type-check
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests
	$(VENV)/bin/mypy

fmt: ## Auto-format and fix lint
	$(VENV)/bin/ruff format src tests
	$(VENV)/bin/ruff check --fix src tests

.PHONY: help install up down build stack stack-logs stack-down migrate revision api worker beat mcp reset-db test-db test lint fmt
