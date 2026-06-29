"""OpenCode SQLite connection-pooling helpers.

Split out of :mod:`ai_r.parsers.opencode` so the parser module carries
less surface.  This module owns the **pure** connection-management
plumbing that is never monkeypatched by the test-suite:

* the per-thread TTL-bounded read-only connection pool
  (:func:`_pooled_conn`);
* :class:`_PoolOpenError` and :func:`_close_pooled`;
* :func:`_iter_dbs`, the generator that walks every readable DB.

The *behavioural* DB layer — :func:`_open_db` (lock retry + /tmp-copy
fallback), :func:`_resolve_db_paths` (multi-location discovery) and the
orphaned-temp-file sweeper (:func:`_sweep_temp_copies` /
:func:`_maybe_sweep_temp_copies`) — stays in :mod:`ai_r.parsers.opencode`
because the tests monkeypatch their module-level dependencies
(``sqlite3``, ``time``, ``glob``, ``_active_temp_conns``, ...) *via the
``opencode`` module object*, and those patches only take effect while
the bodies live in ``opencode``'s namespace.

This module therefore resolves :func:`_open_db` and
:func:`_resolve_db_paths` lazily through ``opencode`` at call time
(avoids an import cycle) so the pool/iterator always invoke the
(patchable) implementations that live there.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from typing import Iterable, Optional, Tuple

# --- TTL-bounded per-thread RO connection pool -------------------------
#
# MCP server tools run sync in a ThreadPoolExecutor → the parser is
# called from multiple threads.  Each thread gets its own cached RO
# connection (``threading.local()``); a cached conn is reused only
# while younger than :data:`_CONN_TTL_SEC`, which bounds the connection
# lifetime and avoids serving stale WAL snapshots indefinitely.  On
# cache miss / expiry / lock the pool delegates (re)opening to
# :func:`_open_db` (in opencode.py), which already implements the
# retry + /tmp-copy fallback contract — that logic is NOT duplicated
# here.
_CONN_TTL_SEC = 60  # seconds a cached RO connection may be reused

_conn_pool = threading.local()


class _PoolOpenError(RuntimeError):
    """Raised internally when :func:`_open_db` returns ``None``."""

    def __init__(self, db_path: str) -> None:
        super().__init__(f"Could not open OpenCode DB: {db_path}")


def _close_pooled(conn: sqlite3.Connection) -> None:
    """Close a (possibly temp-copy) pooled connection, best-effort."""
    try:
        conn.close()
    except Exception:
        pass


def _pooled_conn(db_path: str):
    """Context manager yielding a cached RO connection for ``db_path``.

    Each thread keeps at most one cached connection per DB path.  A
    cached connection is reused only while younger than
    :data:`_CONN_TTL_SEC`; otherwise it is closed and reopened.  All
    (re)opening goes through :func:`_open_db` (opencode.py) so the
    lock-fallback / /tmp-copy contract is preserved.

    The connection is NOT closed on context exit — it stays cached for
    the next call on the same thread.  Closes happen only on expiry
    (next call past TTL) or process teardown (atexit / GC).
    """
    # Lazy import: opencode.py imports from this module at load time, so
    # importing it here (at call time, not module time) breaks the cycle
    # and — crucially — always resolves to whatever
    # ``opencode._open_db`` currently is, so test monkeypatches of
    # ``opencode.sqlite3`` / ``opencode._open_db`` stay in effect.
    from . import opencode

    @contextlib.contextmanager
    def _cm():
        cache = getattr(_conn_pool, "conns", None)
        if cache is None:
            cache = {}
            _conn_pool.conns = cache

        entry = cache.get(db_path)
        conn: Optional[sqlite3.Connection] = None
        if entry is not None:
            cached_conn, opened_at = entry
            if time.monotonic() - opened_at < _CONN_TTL_SEC:
                conn = cached_conn

        if conn is None:
            # Close + drop any stale cached entry, then (re)open.
            if db_path in cache:
                old_conn, _ = cache.pop(db_path)
                _close_pooled(old_conn)
            conn = opencode._open_db(db_path)
            if conn is None:
                # Nothing to yield; let the caller's ``if conn is not
                # None`` / exception handling deal with it.
                raise _PoolOpenError(db_path)
            cache[db_path] = (conn, time.monotonic())

        try:
            yield conn
        except sqlite3.Error:
            # A SQLite error mid-use likely means the cached conn is
            # bad — drop it so the next call reopens.
            cache.pop(db_path, None)
            _close_pooled(conn)
            raise

    return _cm()


def _iter_dbs(
    base_dir: Optional[str], override: Optional[str]
) -> Iterable[Tuple[str, sqlite3.Connection]]:
    """Yield ``(path, conn)`` for every readable OpenCode DB.

    Uses the per-thread TTL-bounded connection pool; the yielded
    connection is NOT closed on exhaustion of the generator (it stays
    cached for reuse).  Connections that fail to open are silently
    skipped.

    Path discovery goes through :func:`_resolve_db_paths` in
    opencode.py (resolved lazily) so test monkeypatches of
    ``ai_r.parsers.opencode._resolve_db_paths`` stay in effect.
    """
    from . import opencode

    for path in opencode._resolve_db_paths(base_dir, override):
        try:
            with _pooled_conn(path) as conn:
                yield path, conn
        except _PoolOpenError:
            continue
