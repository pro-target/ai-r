"""Verify the ai_reader -> ai_r backward-compatibility shim.

The legacy ``ai-local-reader`` skill scripts import the package under its old
name, e.g.::

    from ai_reader.legacy_compat import run_legacy_get_latest_context
    from ai_reader.legacy_compat import run_legacy_agent_audit

These tests guarantee that surface keeps resolving after the ai_reader -> ai_r
rename, and that callers are nudged to migrate via a DeprecationWarning.
"""
import importlib
import sys
import warnings

import ai_r
from ai_r.legacy_compat import (
    is_ai_r_available,
    run_legacy_agent_audit,
    run_legacy_get_latest_context,
)


def _fresh_import_ai_reader():
    """Import ``ai_reader`` with the shim's __init__ re-running each call.

    Removes only the ``ai_reader`` alias entries from ``sys.modules`` (never
    the real ``ai_r`` packages) so the DeprecationWarning fires again.
    """
    for mod in list(sys.modules):
        if mod == "ai_reader" or mod.startswith("ai_reader."):
            del sys.modules[mod]
    return importlib.import_module("ai_reader")


def test_ai_reader_import_emits_deprecation_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _fresh_import_ai_reader()
    assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
        "importing the ai_reader alias must emit a DeprecationWarning"
    )


def test_ai_reader_alias_is_ai_r():
    mod = _fresh_import_ai_reader()
    assert mod is ai_r, "ai_reader must alias the real ai_r package object"


def test_external_import_paths_resolve():
    """The exact imports used by the legacy skill scripts must work."""
    _fresh_import_ai_reader()
    # get_latest_context.py:16
    from ai_reader.legacy_compat import run_legacy_get_latest_context as glc  # noqa: F401
    # agent-audit.py:43
    from ai_reader.legacy_compat import run_legacy_agent_audit as aaa  # noqa: F401
    assert glc is run_legacy_get_latest_context
    assert aaa is run_legacy_agent_audit


def test_shim_reexports_full_legacy_compat_surface():
    _fresh_import_ai_reader()
    from ai_reader.legacy_compat import (
        is_ai_r_available as shim_avail,
        run_legacy_agent_audit as shim_aaa,
        run_legacy_get_latest_context as shim_glc,
    )
    assert shim_avail is is_ai_r_available
    assert shim_aaa is run_legacy_agent_audit
    assert shim_glc is run_legacy_get_latest_context
