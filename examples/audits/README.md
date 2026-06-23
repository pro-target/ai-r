# Audit recipes

Runnable templates that audit a past agent session for dry-run,
completeness, and quality signals.  They use the public
`ai_reader.parsers.<agent>.read_messages` API (see
[`src/ai_reader/parsers/__init__.py`](../../src/ai_reader/parsers/__init__.py))
which returns structured [`Message`](../../src/ai_reader/parsers/models.py)
objects preserving `tool_use` and `tool_result` blocks.

These recipes mirror the behavioural-audit half of `git-auditor`'s
`scout_behavioral_audit`
([`~/.agents/skills/git-auditor/scripts/context_auditor.py`](https://github.com/pro-target/git-auditor)
lines ~393-477): (a) grep the session action log for test-runner commands
to confirm tests ran, and (b) check that changed files appear in the
agent's reasoning.  The difference is that here the audit runs through
the clean `ai-reader` library instead of shelling out to the legacy
`agent-audit.py` wrapper.

## Why the Python API, not MCP

The MCP `read_session` tool deliberately flattens `tool_use` and
`tool_result` blocks into plain text — it is built for consumption by an
LLM summariser, not for structured audit questions like "which tool
invoked the test runner?" or "was this file mentioned in a tool input?".
Answering those needs the un-flattened `Message.tool_use` /
`Message.tool_result` surface, which only the Python `read_messages` API
exposes.  This is exactly the API-coverage gap
that the `read_messages` functions were added to close.

## Recipes

All scripts are self-contained, stdlib-only, Python 3.11+.  Run them
from the repository root with the package importable (e.g. inside the
project venv, or with `PYTHONPATH=src`).

### `dry_run_check.py` — did the agent actually run tests?

Scans every `Message`'s `text`, `tool_use` (name + input), and
`tool_result.content` for test-runner command patterns (`phpunit`,
`artisan test`, `pytest`, `jest`, `vitest`, `playwright`, `npm test`,
`yarn test`, `pnpm test`, `go test`, `docker exec ... test`).  Reports
which runners were detected and the tool surface that matched.  Exits 0
if at least one runner fired, 1 otherwise.

```sh
python examples/audits/dry_run_check.py <uuid> --agent claude
```

### `completeness_check.py` — task drift / coverage

For each changed file, checks whether its basename appears in any
assistant `Message` (`text` or `tool_use` input).  Reports matched /
total and lists unmatched files as potential drift.  Exits 0 if all
files are mentioned, 1 otherwise.

```sh
python examples/audits/completeness_check.py <uuid> --agent claude \
    --file src/foo.py --file tests/test_foo.py
# or, populate the list from staged changes:
python examples/audits/completeness_check.py <uuid> --agent claude --from-git
```

### `quality_check.py` — quality signals

Counts three families of signals across the session:

- **lint/format runs** — `eslint`, `ruff`, `black`, `prettier`, `mypy`,
  `flake8`, `pyright`, `tsc`, `rubocop`.
- **error -> retry loops** — tool results carrying error signatures
  (`Traceback`, `Error:`, `FAILED`, `error TS`) followed by a later
  `tool_use`.
- **explicit verification steps** — `verify`, `--check`, `dry-run`,
  `pytest`, `test`.

Always exits 0 (informational) unless the session cannot be parsed.

```sh
python examples/audits/quality_check.py <uuid> --agent claude
```

## Templates, not law

These are starting points.  Adapt the pattern vocabularies, the matching
granularity (basename vs full path, assistant-only vs all roles), and
the pass/fail thresholds to your team's bar before wiring them into CI.
