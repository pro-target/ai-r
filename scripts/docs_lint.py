#!/usr/bin/env python3
"""Machine gate for the two docs rules humans keep breaking.

1. **English prose stays English.** ``README.md`` and ``docs/methods.md`` (the
   public English SSOT) must carry zero Cyrillic, so a Russian sentence pasted
   during a sync fails CI instead of shipping. Deliberately NOT checked:
   ``docs/scenarios.md`` and the EN gallery, whose Cyrillic is *data*, not prose
   — Russian user turns the parser must handle («Отлично, работает!») and the
   cross-lingual semantic query demo. Their Russian is the point of the example.

2. **Relative links resolve.** Every relative Markdown link target in the public
   docs points at a file that exists. External ``http(s)://`` / ``mailto:`` links
   and bare ``#anchor`` links are out of scope (a link checker that hits the
   network is a flaky gate).

Stdlib only, no install needed — ``make docs-lint`` locally, same script in CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urldefrag

_REPO = Path(__file__).resolve().parents[1]

# Files that must not contain a single Cyrillic character. Their Russian
# counterparts (README.ru.md, docs/methods.ru.md, …) are simply absent here.
_ENGLISH_ONLY = (
    "README.md",
    "CONTRIBUTING.md",
    "docs/methods.md",
)

# Markdown whose relative links are checked: every README (all languages) plus
# the public docs tree.
_LINK_GLOBS = ("README*.md", "CONTRIBUTING.md", "docs/**/*.md")

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")

# The one legitimate Cyrillic in an English file: the language switcher's own
# endonym — ``[Русский](README.ru.md)``. Only the link text is exempt, so a
# Russian sentence elsewhere on that same line still fails.
_LANG_SWITCH_LINK = re.compile(r"\[[^\]]*\]\(README(?:\.[\w-]+)?\.md\)")

# Inline links/images — ``](target)`` — and reference definitions — ``[id]: target``.
_INLINE_LINK = re.compile(r"\]\(\s*([^)\s]+)")
_REF_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(\S+)")

_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)


def _cyrillic_hits() -> list[str]:
    """``path:line: text`` for every Cyrillic-carrying line in an English-only file."""
    hits: list[str] = []
    for rel in _ENGLISH_ONLY:
        path = _REPO / rel
        if not path.is_file():
            hits.append(f"{rel}: MISSING (declared English-only, not on disk)")
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _CYRILLIC.search(_LANG_SWITCH_LINK.sub("", line)):
                hits.append(f"{rel}:{lineno}: {line.strip()[:100]}")
    return hits


def _link_targets(text: str) -> list[tuple[int, str]]:
    """``(line number, raw target)`` for every link in a Markdown source."""
    targets: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        targets.extend((lineno, t) for t in _INLINE_LINK.findall(line))
        targets.extend((lineno, t) for t in _REF_LINK.findall(line))
    return targets


def _broken_links() -> list[str]:
    """``path:line: target`` for every relative link that resolves to nothing."""
    broken: list[str] = []
    for glob in _LINK_GLOBS:
        for path in sorted(_REPO.glob(glob)):
            text = path.read_text(encoding="utf-8")
            for lineno, raw in _link_targets(text):
                target, _ = urldefrag(raw)
                if not target or _EXTERNAL.match(target):
                    continue  # external URL, mailto:, or a bare #anchor
                resolved = (path.parent / unquote(target)).resolve()
                if not resolved.exists():
                    rel = path.relative_to(_REPO)
                    broken.append(f"{rel}:{lineno}: {raw}")
    return broken


def main() -> int:
    cyrillic = _cyrillic_hits()
    broken = _broken_links()

    if cyrillic:
        print("FAIL: Cyrillic in English-only docs")
        for hit in cyrillic:
            print(f"  {hit}")
    if broken:
        print("FAIL: broken relative links")
        for hit in broken:
            print(f"  {hit}")

    if cyrillic or broken:
        print(f"\ndocs-lint: {len(cyrillic)} Cyrillic, {len(broken)} broken links")
        return 1

    print(
        f"docs-lint OK — {len(_ENGLISH_ONLY)} English-only files clean, "
        "all relative links resolve"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
