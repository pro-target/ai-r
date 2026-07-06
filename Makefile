.PHONY: test test-hermetic test-host lint

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

lint:
	python -c "import ai_r, ai_r.cli, ai_r.mcp_server, ai_r.parsers"
