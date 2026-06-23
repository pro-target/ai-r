"""Backward-compatibility alias: the package was renamed ``ai_reader`` -> ``ai_r``.

External tools that still ``import ai_reader...`` (notably the legacy
``ai-local-reader`` skill scripts ``get_latest_context.py`` and
``agent-audit.py``, which delegate session reading to the ``ai-r`` CLI via
:mod:`ai_r.legacy_compat`) are transparently redirected to ``ai_r``.

A ``DeprecationWarning`` is emitted on first import so callers know to
update their imports. This shim exists precisely so the ai_reader -> ai_r
rename does not silently break those external consumers: without it their
``from ai_reader.legacy_compat import ...`` raises ``ModuleNotFoundError``
and they fall back to their own slower code paths.

Only the surface the external scripts rely on is redirected (the top-level
package and ``ai_reader.legacy_compat``).
"""
from __future__ import annotations

import importlib
import sys
import warnings

warnings.warn(
    "the 'ai_reader' package was renamed to 'ai_r'; "
    "update imports from 'ai_reader...' to 'ai_r...'",
    DeprecationWarning,
    stacklevel=2,
)

# Redirect the top-level package and the submodule the legacy skill scripts
# import. Replacing sys.modules entries is the documented mechanism behind
# package-rename shims: the import system returns whatever these entries hold
# after the package's code runs.
_ai_r_pkg = importlib.import_module("ai_r")
_ai_r_compat = importlib.import_module("ai_r.legacy_compat")

sys.modules[__name__ + ".legacy_compat"] = _ai_r_compat
sys.modules[__name__] = _ai_r_pkg
