"""Client SDK for the core service.

Every face that is not the API itself goes through this: the CLI, the MCP
server, and whatever comes next. Adding a face means writing a transport, not
re-deriving endpoints.
"""

from books.sdk.client import BooksAPIError, BooksClient

__all__ = ["BooksAPIError", "BooksClient"]
