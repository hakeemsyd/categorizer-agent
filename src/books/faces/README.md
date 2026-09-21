# Faces

A *face* translates one transport into calls on the core service. Faces hold no
business logic — that is what keeps "add Slack" a one-directory change instead
of a redesign.

```
        CLI        MCP        (Slack, web UI, …)
          \         |          /
           \        |         /
            books.sdk.BooksClient        ← one REST client, shared
                    |
         books.faces.api  (FastAPI)      ← the only face that touches the core
                    |
              books.core                 ← all the logic lives here
```

| Face | Module | Runs as |
|---|---|---|
| REST API | `books.faces.api` | `uvicorn books.faces.api.main:app` |
| CLI | `books.faces.cli` | `books …` |
| MCP | `books.faces.mcp` | `python -m books.faces.mcp.server` (stdio) |

## Adding a face

1. Create `books/faces/<name>/`.
2. Use `books.sdk.BooksClient` for every read and write. If the endpoint you
   need does not exist, add it to `books.faces.api.routes` **and** the SDK — not
   to your face.
3. Never import `books.core.*` from a non-API face, and never import a provider
   SDK anywhere outside `books.providers`.
4. Attribute human writes with a real `actor` string (`slack:U123`,
   `cli:hakeem`). It lands in `categorization_history` and is how a correction
   is told apart from an agent guess.

## The rule that matters

Only `books.core.categorization.apply_category` writes
`transactions.category_id`. If a face ever needs to "just set the category", it
is asking for a new core function, not a shortcut.
