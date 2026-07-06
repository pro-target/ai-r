## Summary

<!-- What does this PR do and why? -->

## Linked issues

<!-- "Fixes #123" or "Closes #456". Use "Refs #789" for non-blocking. -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change
- [ ] Documentation
- [ ] Refactor / cleanup
- [ ] CI / tooling

## How was it tested?

- [ ] `pytest --cov=src/ai_r` passes locally
- [ ] New tests added (if applicable)
- [ ] Coverage stays ≥ 80% (`pyproject.toml` enforces this)
- [ ] LLM e2e scenarios pass for changed functionality — GO / GO-with-caveats
      on every runnable scenario ([docs/scenarios.md](./docs/scenarios.md);
      required when the public surface changed)
- [ ] `install.sh` dry-run still parses (`DRY_RUN=1 bash install.sh`)

## Checklist

- [ ] Branch rebased on `main`
- [ ] Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, …)
- [ ] Public functions / classes have docstrings
- [ ] No new top-level dependencies added (or justified in PR body)
- [ ] `CHANGELOG.md` updated (under `## [Unreleased]`)
- [ ] `CONTEXT.md` updated if the domain language changed
- [ ] Docs under `docs/` updated if behaviour changed
- [ ] I have read [`CONTRIBUTING.md`](./CONTRIBUTING.md)

## Security considerations

<!-- Does this PR touch parsers that read untrusted session data, or anywhere
a malicious session file could affect the host (path resolution, file I/O,
/tmp handling)? If so, describe. See docs/security.md for the trust boundary. -->

## Screenshots / output

<!-- Optional: paste CLI output, MCP tool result, or screenshots. -->
