"""Session exporters — render :class:`Session` data into external formats.

Exports are read-only, opt-in post-processing layers over the parser
package.  Adding a new exporter is a one-file operation: drop a module
under this package and re-export it from :mod:`ai_reader.exporters`.

Modules:
    rounds:   Markdown Round/CHANGELOG emitter for ``work/CHANGELOG.md``.
"""

from .rounds import session_to_rounds

__all__ = ["session_to_rounds"]
