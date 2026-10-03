"""Provider lookup by name.

Items carry their provider in a column, so adding an aggregator never migrates
existing rows.
"""

from __future__ import annotations

from collections.abc import Callable

from books.core.errors import ConfigurationError
from books.providers.base import TransactionProvider

_FACTORIES: dict[str, Callable[[], TransactionProvider]] = {}


def register_provider(name: str, factory: Callable[[], TransactionProvider]) -> None:
    _FACTORIES[name] = factory


def get_provider(name: str) -> TransactionProvider:
    try:
        factory = _FACTORIES[name]
    except KeyError:
        known = ", ".join(sorted(_FACTORIES)) or "<none registered>"
        raise ConfigurationError(f"Unknown provider {name!r}. Registered: {known}") from None
    return factory()


def available_providers() -> list[str]:
    return sorted(_FACTORIES)


def _register_builtin() -> None:
    from books.providers.fake import FakeProvider
    from books.providers.fintable import FintableProvider

    register_provider("fintable", FintableProvider)
    register_provider("fake", FakeProvider)


_register_builtin()
