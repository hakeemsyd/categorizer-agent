"""Which business the CLI is working on.

With one business everything is unambiguous. With several, passing
``--business "Some Long Name"`` to every command gets old fast, so the choice
is remembered on disk.

Keyed by API base URL: pointing the CLI at a different deployment must not
silently carry over a business id that does not exist there.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_VAR = "BOOKS_BUSINESS"


@dataclass(frozen=True)
class SelectedBusiness:
    id: str
    name: str


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "books" / "context.json"


def _load() -> dict[str, Any]:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing, unreadable or corrupt: the context is a convenience, never
        # a prerequisite. Behave as though nothing was ever selected.
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so an interrupted write cannot leave a truncated file.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _key(base_url: str) -> str:
    return base_url.rstrip("/")


def get_business(base_url: str) -> SelectedBusiness | None:
    entry = (_load().get("businesses") or {}).get(_key(base_url))
    if isinstance(entry, dict) and entry.get("id"):
        return SelectedBusiness(id=str(entry["id"]), name=str(entry.get("name", "")))
    return None


def set_business(base_url: str, *, business_id: str, name: str) -> None:
    data = _load()
    businesses = data.setdefault("businesses", {})
    businesses[_key(base_url)] = {"id": business_id, "name": name}
    _save(data)


def clear_business(base_url: str) -> bool:
    data = _load()
    businesses = data.get("businesses") or {}
    if _key(base_url) not in businesses:
        return False
    del businesses[_key(base_url)]
    _save(data)
    return True


def env_business() -> str | None:
    """``BOOKS_BUSINESS`` overrides the saved choice, for scripts and CI."""
    return os.environ.get(ENV_VAR) or None
