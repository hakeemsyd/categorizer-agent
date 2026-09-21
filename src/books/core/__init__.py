"""The core service: all business logic lives here.

Faces (API, CLI, MCP, workers) orchestrate these functions; they never
re-implement them. In particular every category write goes through
:func:`books.core.categorization.apply_category`.
"""
