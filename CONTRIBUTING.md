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
4. **Run the full test suite + linters locally:**
   ```bash
   pip install -e ".[dev]"
   pytest --cov=src/ai_r
   ```
   Coverage must stay ≥ 80% (`pyproject.toml` enforces this in CI).
5. **Run the LLM e2e acceptance scenarios** whenever the change adds
   or modifies functionality (a new MCP tool or parameter, any
   behaviour change on the public surface). Both gates must pass —
   the pytest suite AND the scenario run:
   - update [docs/scenarios.md](./docs/scenarios.md) first — it is the
     SSOT for the public surface; CI fails when an MCP tool has no
     scenario (`tests/test_docs_sync.py`);
   - then have an LLM agent execute the affected scenarios against a
     **live** MCP server (see *How to run* in `docs/scenarios.md`).
     Every runnable scenario must resolve **GO** or
     **GO-with-caveats**; `[needs-real-vault]` scenarios without the
     required vault data are skipped, not failed. A **NO-GO blocks
     the merge**.
6. **Conventional Commits.** Allowed prefixes: `feat:`, `fix:`,
   `docs:`, `test:`, `refactor:`, `chore:`, `ci:`. Example:
   `feat(parsers): add Gemini parser`.
7. **Open a PR.** The PR template will guide you. CI must be green.
   A maintainer will review within a few days.

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
2. Create `src/ai_r/parsers/<agent>.py` exporting `list_sessions`,
   `read_session`, `search`, `session_exists`.
3. Re-export the module from `src/ai_r/parsers/__init__.py`.
4. Add a `tests/test_parsers/test_<agent>.py` with fixtures.

## Reporting a security issue

Please **do not** open a public issue for vulnerabilities. Email
wm-k@mail.ru with `SECURITY` in the subject. We respond within 7 days.

## License

By contributing, you agree that your contributions will be licensed
under the MIT License. See [LICENSE](./LICENSE).
