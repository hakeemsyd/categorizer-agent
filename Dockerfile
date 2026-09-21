# syntax=docker/dockerfile:1
#
# One image, three roles. The API, the Celery worker and Beat all run from this
# same build and differ only by their command — see docker-compose.yml.

# --- build stage: resolve and install dependencies into a venv ---------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
RUN uv venv /opt/venv

# Dependencies first: this layer stays cached until pyproject.toml changes,
# so editing source does not re-resolve the dependency tree.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python -r pyproject.toml

COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --no-deps .

# --- runtime stage -----------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root: the service holds bank credentials and a decryption key.
# /app is created and chowned here, before WORKDIR: a bare WORKDIR leaves the
# directory root-owned, and Celery Beat writes its schedule file into it.
RUN groupadd --system --gid 1001 books \
 && useradd --system --uid 1001 --gid books --create-home books \
 && mkdir -p /app \
 && chown books:books /app

COPY --from=builder --chown=books:books /opt/venv /opt/venv

WORKDIR /app
# Migrations and the seed script are not part of the wheel but are needed at
# runtime (`alembic upgrade head` on deploy).
COPY --chown=books:books alembic.ini ./
COPY --chown=books:books db/ db/
COPY --chown=books:books scripts/ scripts/

USER books
EXPOSE 8000

# Overridden for the worker and beat roles.
CMD ["uvicorn", "books.faces.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
