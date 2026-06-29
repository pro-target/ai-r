"""CLI package for ai-r.

Re-exports :func:`main` and :func:`build_parser` so the public surface
stays identical to the old monolithic ``ai_r.cli`` module:

    from ai_r.cli import main, build_parser

The implementation lives in :mod:`ai_r.cli.main` (parser + dispatcher),
:mod:`ai_r.cli.shared` (helpers/constants), and :mod:`ai_r.cli.commands`.
"""

from __future__ import annotations

from ai_r.cli.main import build_parser, main

__all__ = ["main", "build_parser"]
