# Search operators

`search_sessions` (MCP) and `ai-r search` (CLI) share the same query
parser and operator parameter. Default behaviour (`scope="title"`,
`operator="AND"`, `limit=50`) is the historical title-only substring
search.

## Query syntax

| Form | Example | Meaning |
|---|---|---|
| Bare words | `pwa manifest` | Both terms (operator controls how). |
| Quoted phrase | `"exact phrase"` | Single literal term. |
| Negative prefix | `-claude` | Google-style: this term must NOT appear. |

Words `AND`, `OR`, and `NOT` inside the query are literal search terms.
Boolean behaviour is selected with `--operator and|or|not` (CLI) or
`operator="AND"|"OR"|"NOT"` (MCP).

## Operator modes (controls how positive terms combine)

| Mode | `pwa manifest` semantics | `pwa -claude` semantics |
|---|---|---|
| `AND` (default) | both must appear | `pwa` appears, `claude` does not |
| `OR` | at least one appears | one of `pwa` appears, `claude` does not |
| `NOT` | neither appears | neither `pwa` nor `claude` appears |

## Scope modes

| Scope | Where the search runs |
|---|---|
| `title` (default) | `session.title` only — matches the historical title-only behaviour. |
| `body` | message text + `tool_use[*].input` + `tool_result[*].content` for every session. |
| `all` | title OR body. |

When `scope` is `body` or `all` and a match is found, the result includes
a `snippet` field (CLI: printed in the table) — the first matching
excerpt, up to 200 characters. Results are BM25-ranked by default
(`sort=relevance`); pass `sort=date` to order by recency.

**Performance note**: `body` and `all` invoke `read_messages` on every
candidate session. On large vaults the first run can be slow; raise
`--limit` to keep the result set bounded while iterating.

## MCP example

```python
search_sessions(
    query='pwa -claude',
    agent='claude',
    scope='body',
    operator='AND',
    limit=20,
)
```

## CLI examples

```bash
# title-only (legacy, still default)
ai-r search "refactor"

# body search, all terms must appear, exclude claude
ai-r search "pwa manifest -claude" --scope body --operator and

# body search, any term, max 5 results
ai-r search "pwa manifest" --scope body --operator or --limit 5

# everything containing neither of these terms
ai-r search "auth login" --scope body --operator not
```
