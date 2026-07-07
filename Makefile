.PHONY: test test-hermetic test-host lint usage-audit

# Full suite as developers run it locally (host data is used where present).
test:
	pytest --cov=src/ai_r --cov-fail-under=85 --cov-report=term

# Mimics a clean CI runner: empty HOME + host tests deselected. Any
# non-host test that secretly needs real session data fails HERE, locally,
# before it can turn main red. Run this before pushing.
test-hermetic:
	HOME=$$(mktemp -d) pytest -m "not host" --cov=src/ai_r --cov-fail-under=85 --cov-report=term

# Only the host-integration tests (need real ~/.claude, ~/.codex, … data).
test-host:
	pytest -m host

# Mirrors the CI `lint` job exactly (import smoke + ruff + mypy) so a red lint
# is caught HERE, before push — not on CI. Run before every push.
lint:
	python -c "import ai_r, ai_r.cli, ai_r.mcp_server, ai_r.parsers"
	ruff check src/
	mypy src/

# Self-referential usage audit (CONTRIBUTING → Releasing): which ai-r verbs/params were
# actually called since the last release. Reads a real vault. Run once per
# release; a zero-call declared param is a tombstone *candidate* (human decides).
# Override the date: make usage-audit SINCE=2026-07-05
SINCE ?= $(shell date -d '30 days ago' +%F 2>/dev/null || date +%F)
usage-audit:
	python scripts/usage_audit.py --since $(SINCE)
