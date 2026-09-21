"""Domain errors.

The API face maps these onto HTTP status codes in one place
(``books.faces.api.main``), so core code never imports FastAPI.
"""

from __future__ import annotations


class BooksError(Exception):
    """Base class for every expected failure in the core."""


class ConfigurationError(BooksError):
    """A required setting is missing or malformed."""


class NotFoundError(BooksError):
    """A referenced row does not exist."""


class ValidationError(BooksError):
    """The request is well-formed but not valid for this tenant/business."""


class ProviderError(BooksError):
    """The upstream aggregator failed or rejected the call."""


class WebhookVerificationError(BooksError):
    """A webhook could not be attributed to the provider."""
