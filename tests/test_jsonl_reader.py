"""Hermetic tests for the shared guarded JSONL reader (``iter_jsonl_records``).

Covers the caps that keep a single pathological file from exhausting memory:
per-line skip, recovery after an over-long line, and — the regression the
external audit flagged — a newline-free/long-tailed file must NOT let the
buffer regrow chunk-by-chunk up to ``max_total_bytes``.
"""

from __future__ import annotations

import tracemalloc

from ai_r.parsers._common import iter_jsonl_records


def _write(tmp_path, text: str):
    p = tmp_path / "s.jsonl"
    p.write_text(text, encoding="utf-8")
    return p


def test_reads_all_valid_records(tmp_path) -> None:
    p = _write(tmp_path, '{"a":1}\n{"b":2}\n{"c":3}\n')
    assert list(iter_jsonl_records(p)) == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_skips_blank_invalid_and_non_dict(tmp_path) -> None:
    p = _write(tmp_path, '{"a":1}\n\n  \nnot json\n[1,2,3]\n{"b":2}\n')
    assert list(iter_jsonl_records(p)) == [{"a": 1}, {"b": 2}]


def test_final_line_without_newline(tmp_path) -> None:
    p = _write(tmp_path, '{"a":1}\n{"b":2}')
    assert list(iter_jsonl_records(p)) == [{"a": 1}, {"b": 2}]


def test_over_long_line_skipped_then_recovers(tmp_path) -> None:
    """A line past the cap is skipped whole; the next valid line still parses."""
    huge = "x" * 5000
    p = _write(tmp_path, f'{{"huge":"{huge}"}}\n{{"ok":1}}\n')
    assert list(iter_jsonl_records(p, max_line_bytes=1024)) == [{"ok": 1}]


def test_multichunk_over_long_then_recovers(tmp_path) -> None:
    """An over-long line spanning MANY 1 MiB read-chunks is discarded, and a
    valid record after its newline still parses — exercises the over_long
    state persisting across chunks (where the buffer used to regrow)."""
    blob = "x" * (3 * 1024 * 1024)  # 3 MiB, no newline -> spans 3+ chunks
    p = _write(tmp_path, f'{blob}\n{{"ok":1}}\n')
    assert list(iter_jsonl_records(p, max_line_bytes=1024)) == [{"ok": 1}]


def test_pure_newline_free_blob_yields_nothing(tmp_path) -> None:
    p = _write(tmp_path, "y" * (3 * 1024 * 1024))
    assert list(iter_jsonl_records(p, max_line_bytes=1024)) == []


def test_total_cap_stops_reading(tmp_path) -> None:
    """The cumulative cap stops reading (coarse, at 1 MiB read-chunk
    granularity): a file whose single chunk already exceeds the cap yields
    nothing rather than being slurped."""
    p = _write(tmp_path, '{"a":1}\n{"b":2}\n{"c":3}\n')
    assert list(iter_jsonl_records(p, max_total_bytes=8)) == []


def test_newline_free_file_does_not_regrow_buffer(tmp_path) -> None:
    """Regression (audit): a 6 MiB newline-free file must stay bounded to a
    couple of read-chunks, not accumulate toward max_total_bytes (~1 GiB).
    Peak traced allocation is asserted well under the file size."""
    p = _write(tmp_path, "z" * (16 * 1024 * 1024))
    tracemalloc.start()
    tracemalloc.reset_peak()
    result = list(iter_jsonl_records(p, max_line_bytes=1024))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result == []
    # New behaviour: buffer bounded to ~1 read-chunk regardless of file size
    # (~4 MiB traced). Old (buggy) behaviour let pending grow to ~16 MiB. An
    # 8 MiB line cleanly separates the two and is non-flaky.
    assert peak < 8 * 1024 * 1024
