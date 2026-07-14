# Contributing to ai-r

Thanks for your interest! This project is small enough that the
fastest path from idea to merge is:

1. **Open an issue first** for non-trivial changes. Discuss the
   approach before writing code. Issues tagged `good first issue` are
   safe to grab.
2. **Fork and branch.** Branch names: `feat/<short-name>`,
   `fix/<short-name>`, `docs/<short-name>`.
3. **Write the change + tests.** New parsers need unit tests with
   fixtures under `tests/fixtures/`.
4. **Run the gates locally — before every push:**
   ```bash
   pip install -e ".[dev]"   # dev setup; install.sh is for *using* ai-r, not hacking on it
   make test                 # full suite, uses your host session data where present
   make test-hermetic        # same suite on an EMPTY HOME, host tests deselected
   make lint                 # import smoke + `ruff check src/` + `mypy src/`
   make docs-lint            # no Cyrillic in the English docs, no broken relative links
   ```
   Each target mirrors a CI job exactly, so a red gate (a `mypy` error,
   a broken link) surfaces here — not after the push. Coverage must stay
   ≥ 85%. `make test-hermetic` is the one people forget: tests must pass
   on a machine with **zero** local session data, so anything that
   secretly reads your real `~/.claude` fails there. Host-dependent tests
   are marked `@pytest.mark.host` and skipped, never failed, on a bare
   host.
5. **Keep the docs in the same commit as the code.** These are merge
   gates, not chores — CI enforces the first one, reviewers the rest:
   - `docs/methods.md` (English SSOT; mirror `docs/methods.ru.md`) —
     when the public surface changes;
   - [docs/scenarios.md](./docs/scenarios.md) — every MCP tool needs a
     scenario (`tests/test_docs_sync.py` fails otherwise);
   - [docs/architecture.md](./docs/architecture.md) — a new
     `src/ai_r/*.py` subsystem, or a reversal of an earlier decision,
     needs its ADR entry.

   Prose in `README*.md` is a separate, human-reviewed flow — do not
   bundle it into a code PR.
6. **Run the LLM e2e acceptance scenarios** whenever the change adds
   or modifies functionality (a new MCP tool or parameter, any
   behaviour change on the public surface). Both gates must pass — the
   pytest suite AND the scenario run. With `docs/scenarios.md` updated
   (step 5), have an LLM agent execute the affected scenarios against a
   **live** MCP server (see *How to run* in `docs/scenarios.md`). Every
   runnable scenario must resolve **GO** or **GO-with-caveats**;
   `[needs-real-vault]` scenarios without the required vault data are
   skipped, not failed. A **NO-GO blocks the merge**.
7. **Conventional Commits.** Allowed prefixes: `feat:`, `fix:`,
   `docs:`, `test:`, `refactor:`, `chore:`, `ci:`. Example:
   `feat(parsers): add Gemini parser`. Keep commits atomic — one
   concern per commit, code and its docs together.
8. **Open a PR.** The PR template will guide you. CI must be green.
   A maintainer will review within a few days.

## Releasing

Once per release (not per PR), run the **self-referential usage audit** —
ai-r reading its own development history to see which verbs/parameters callers
actually used since the last tag:

```bash
make usage-audit SINCE=<previous-release-date>   # e.g. 2026-07-05
```

A zero-call declared parameter is a tombstone *candidate*, not an automatic
deletion — a human decides, and the tool already excludes safety-default
parameters (`redact` and kin) and flags thin samples / single-agent coverage.
An `!! UNDECLARED PARAMS USED` line means a caller passed a parameter a verb
does not declare; post the fail-loud fix (`_StrictArgsFastMCP`) live calls are
rejected, so any such line is historical. Rationale: the *ADR: fail-loud on
unknown MCP arguments* in [docs/architecture.md](./docs/architecture.md).

## Local-dev MCP setup

For local-dev MCP setup (registering `ai-r-mcp` so your editor can
drive it), see **MCP registration** in [README.md](./README.md).

## Style

- Python 3.11+ idioms (`X | None`, `match`, `dataclass(slots=True)`).
- No comments in code unless they explain a non-obvious decision.
  Module docstrings are welcome and brief.
- Imports: stdlib first, third-party second, local third.
  One blank line between groups.
- All public functions and classes get a docstring.

## Adding a new agent parser

See [docs/parsers.md](./docs/parsers.md). Summary:
1. Add a value to `AgentName` in `src/ai_r/parsers/models.py`.
2. Create `src/ai_r/parsers/<agent>.py` exporting the five parser
   functions — `list_sessions`, `read_session`, `read_messages`,
   `search`, `session_exists` (see [docs/architecture.md](./docs/architecture.md)).
3. Re-export the module from `src/ai_r/parsers/__init__.py`.
4. Add a `tests/test_parsers/test_<agent>.py` with fixtures.

## Reporting a security issue

Please **do not** open a public issue for vulnerabilities. Email
wm-k@mail.ru with `SECURITY` in the subject. We respond within 7 days.

## License

By contributing, you agree that your contributions will be licensed
under the MIT License. See [LICENSE](./LICENSE).
