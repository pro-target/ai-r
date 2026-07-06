"""F5.1 semantic re-ranking: hermetic tests for both branches.

Both worlds are exercised WITHOUT the real model:

* "dependencies present" — a fake embedder (deterministic vectors) is
  injected into the module's lazy-loader state, or fake ``onnxruntime`` /
  ``tokenizers`` / ``numpy`` modules are planted in ``sys.modules`` so the
  real loading/embedding plumbing runs end-to-end;
* "dependencies absent" — imports are force-blocked, proving the honest
  BM25 fallback (clear reason, never a crash).

An optional ``@pytest.mark.host`` smoke test runs the real model when the
host actually has the extra installed and the files downloaded.
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path
from typing import Iterator, Sequence

import pytest

from ai_r import semantic
from ai_r.mcp_server import query as mcp_query, search_sessions
from ai_r.semantic import (
    _Embedder,
    _is_idle,
    _mean_pool_normalize,
    _minmax,
    _thread_cap,
    release_if_idle,
    semantic_order,
    semantic_status,
)


# ---------------------------------------------------------------------------
# Isolation: every test starts with a fresh probe state and no model-dir env.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_semantic_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("AI_R_SEMANTIC_MODEL_DIR", raising=False)
    monkeypatch.delenv("AI_R_SEMANTIC_THREADS", raising=False)
    monkeypatch.delenv("AI_R_SEMANTIC_IDLE_SEC", raising=False)
    semantic._reset_state()
    yield
    semantic._reset_state()


def _force_unavailable(reason: str = "semantic dependencies (onnxruntime, "
                       'tokenizers) are not installed — pip install "ai-r[semantic]"') -> None:
    """Pin the lazy loader to the 'missing' outcome, whatever the host has."""
    semantic._STATE.update({"probed": True, "embedder": None, "reason": reason})


class _FakeEmbedder:
    """Deterministic 2-D unit vectors keyed by text content.

    * a ``query: `` text → (1, 0);
    * a passage containing ``segfault`` → nearly parallel to the query;
    * anything else → nearly orthogonal.
    """

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(list(texts))
        out = []
        for t in texts:
            if t.startswith(semantic.QUERY_PREFIX):
                out.append([1.0, 0.0])
            elif "segfault" in t:
                out.append([0.9848, 0.1736])  # ~10° from the query
            else:
                out.append([0.1736, 0.9848])  # ~80° from the query
        return out


def _install_fake_embedder(fail: bool = False) -> _FakeEmbedder:
    fake = _FakeEmbedder(fail=fail)
    semantic._STATE.update({"probed": True, "embedder": fake, "reason": None})
    return fake


# ---------------------------------------------------------------------------
# Pure math helpers
# ---------------------------------------------------------------------------


def test_minmax_normalizes_to_unit_range() -> None:
    assert _minmax([2.0, 4.0, 3.0]) == [0.0, 1.0, 0.5]


def test_minmax_flat_input_is_neutral() -> None:
    # A flat signal must not pretend to rank anything: 0.5 lets the OTHER
    # blend component decide alone.
    assert _minmax([3.0, 3.0, 3.0]) == [0.5, 0.5, 0.5]
    assert _minmax([]) == []


def test_mean_pool_normalize_respects_mask_and_unit_norm() -> None:
    tokens = [[2.0, 0.0], [4.0, 0.0], [100.0, 100.0]]
    mask = [1, 1, 0]  # the padded third token must not count
    vec = _mean_pool_normalize(tokens, mask)
    assert vec == pytest.approx([1.0, 0.0])
    norm = sum(v * v for v in vec) ** 0.5
    assert norm == pytest.approx(1.0)


def test_mean_pool_normalize_zero_mask_is_zero_vector() -> None:
    assert _mean_pool_normalize([[1.0, 2.0]], [0]) == [0.0, 0.0]


# ---------------------------------------------------------------------------
# semantic_order: ranking semantics with a fake embedder
# ---------------------------------------------------------------------------


def test_semantic_order_reranks_by_meaning() -> None:
    _install_fake_embedder()
    docs = [
        "crash crash crash crash",       # strongest word match, meaning far
        "we hit a segfault backtrace",   # weak word match, meaning close
        "crash noise",                   # middling
    ]
    bm25 = [3.0, 1.0, 2.0]
    order, info = semantic_order("crash", docs, bm25)
    # meaning (minmax): [0, 1, 0] -> blend 0.25/0.75/(0*0.75+0.5*0.25)
    assert order is not None
    assert order[0] == 1, "the meaning-close doc must win despite lower BM25"
    assert info["active"] is True
    assert info["model"] == semantic.MODEL_NAME
    assert info["candidates"] == 3
    assert info["weight"] == semantic.SEMANTIC_WEIGHT


def test_semantic_order_applies_e5_prefixes() -> None:
    fake = _install_fake_embedder()
    semantic_order("crash", ["some doc"], [1.0])
    query_batch, passage_batch = fake.calls
    assert query_batch == [semantic.QUERY_PREFIX + "crash"]
    assert passage_batch[0].startswith(semantic.PASSAGE_PREFIX)


def test_semantic_order_candidate_budget_tail_keeps_bm25_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_embedder()
    monkeypatch.setattr(semantic, "CANDIDATES", 2)
    docs = [
        "crash words only",        # bm25 4.0 -> pool
        "segfault mention",        # bm25 3.0 -> pool, wins pool by meaning
        "segfault but weak bm25",  # bm25 2.0 -> tail (meaning ignored)
        "noise",                   # bm25 1.0 -> tail
    ]
    order, info = semantic_order("crash", docs, [4.0, 3.0, 2.0, 1.0])
    assert order is not None
    assert info["candidates"] == 2
    # Pool re-ranked by meaning (doc 1 beats doc 0); tail 2,3 untouched in
    # BM25 order even though doc 2 mentions segfault.
    assert order == [1, 0, 2, 3]


def test_semantic_order_empty_docs_active_noop() -> None:
    _install_fake_embedder()
    order, info = semantic_order("crash", [], [])
    assert order == []
    assert info["active"] is True
    assert info["candidates"] == 0


def test_semantic_order_unavailable_falls_back_with_reason() -> None:
    _force_unavailable()
    order, info = semantic_order("crash", ["a", "b"], [1.0, 2.0])
    assert order is None
    assert info["active"] is False
    assert info["fallback"] == "bm25"
    assert "ai-r[semantic]" in info["reason"]


def test_semantic_order_embed_failure_degrades_not_raises() -> None:
    _install_fake_embedder(fail=True)
    order, info = semantic_order("crash", ["a"], [1.0])
    assert order is None
    assert info["active"] is False
    assert "boom" in info["reason"]
    assert info["fallback"] == "bm25"


# ---------------------------------------------------------------------------
# Loader: missing deps / missing files / fake-runtime success
# ---------------------------------------------------------------------------


def test_status_reports_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None entry in sys.modules makes ``import onnxruntime`` raise —
    # deterministic on hosts both with and without the package.
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    monkeypatch.setitem(sys.modules, "tokenizers", None)
    status = semantic_status()
    assert status["available"] is False
    assert "ai-r[semantic]" in status["reason"]


def _fake_runtime_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> types.ModuleType:
    """Plant importable fake onnxruntime/tokenizers/numpy in sys.modules.

    Returns the fake ``onnxruntime`` module so a test can inspect the
    ``InferenceSession.last_options`` spy after loading.
    """

    class _FakeInput:
        def __init__(self, name: str) -> None:
            self.name = name

    class _FakeSessionOptions:
        def __init__(self) -> None:
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0

    class _FakeSession:
        # Records the SessionOptions of the LAST construction so a test can
        # assert the thread cap actually reached onnxruntime.
        last_options: object | None = None

        def __init__(
            self,
            path: str,
            sess_options: object | None = None,
            providers: list[str] | None = None,
        ) -> None:
            self.path = path
            self.sess_options = sess_options
            type(self).last_options = sess_options

        def get_inputs(self) -> list[_FakeInput]:
            return [
                _FakeInput("input_ids"),
                _FakeInput("attention_mask"),
                _FakeInput("token_type_ids"),
            ]

        def run(self, _outputs: object, feeds: dict) -> list:
            assert set(feeds) == {"input_ids", "attention_mask", "token_type_ids"}
            batch = feeds["input_ids"]
            # One constant token vector per position: pooled = [1,0,0] etc.
            return [[[[1.0, 0.0] for _ in row] for row in batch]]

    class _FakeEncoding:
        def __init__(self, text: str) -> None:
            self.ids = [1, 2]
            self.attention_mask = [1, 1]

    class _FakeTokenizer:
        def enable_truncation(self, max_length: int) -> None:
            assert max_length == 512

        def enable_padding(self) -> None:
            pass

        def encode_batch(self, texts: list[str]) -> list[_FakeEncoding]:
            return [_FakeEncoding(t) for t in texts]

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.InferenceSession = _FakeSession  # type: ignore[attr-defined]
    fake_ort.SessionOptions = _FakeSessionOptions  # type: ignore[attr-defined]

    fake_tok = types.ModuleType("tokenizers")

    class _TokenizerFactory:
        @staticmethod
        def from_file(path: str) -> _FakeTokenizer:
            return _FakeTokenizer()

    fake_tok.Tokenizer = _TokenizerFactory  # type: ignore[attr-defined]

    fake_np = types.ModuleType("numpy")
    fake_np.int64 = "int64"  # type: ignore[attr-defined]
    fake_np.array = lambda x, dtype=None: x  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "tokenizers", fake_tok)
    monkeypatch.setitem(sys.modules, "numpy", fake_np)
    return fake_ort


def test_status_reports_missing_model_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_runtime_modules(monkeypatch)
    monkeypatch.setenv("AI_R_SEMANTIC_MODEL_DIR", str(tmp_path / "empty"))
    status = semantic_status()
    assert status["available"] is False
    assert "install.sh" in status["reason"]
    assert str(tmp_path / "empty") in status["reason"]


def test_loader_and_embed_through_fake_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The REAL loading + embedding plumbing runs against a fake runtime."""
    _fake_runtime_modules(monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model_qint8_avx512_vnni.onnx").write_bytes(b"onnx")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AI_R_SEMANTIC_MODEL_DIR", str(model_dir))

    status = semantic_status()
    assert status == {"available": True, "model": semantic.MODEL_NAME}

    embedder = semantic._STATE["embedder"]
    assert isinstance(embedder, _Embedder)
    vectors = embedder.embed(["hello", "мир"])
    assert len(vectors) == 2
    # NB: no pytest.approx here — it would poke the FAKE numpy module
    # planted in sys.modules.  Plain-float tolerance instead.
    for vec in vectors:
        assert len(vec) == 2
        assert abs(vec[0] - 1.0) < 1e-9 and abs(vec[1]) < 1e-9


def test_model_dir_env_override_and_home_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AI_R_SEMANTIC_MODEL_DIR", str(tmp_path / "custom"))
    assert semantic.model_dir() == tmp_path / "custom"
    monkeypatch.delenv("AI_R_SEMANTIC_MODEL_DIR")
    monkeypatch.setenv("AI_R_HOME", str(tmp_path / "home"))
    assert semantic.model_dir() == (
        tmp_path / "home" / ".cache" / "ai-r" / "semantic"
        / "multilingual-e5-small"
    )


# ---------------------------------------------------------------------------
# MCP surface: search_sessions / query with sort="semantic"
# ---------------------------------------------------------------------------


def _write_claude_body_session(
    tmp_sessions_dir: Path, uuid: str, user_text: str, when: str
) -> None:
    records = [
        {
            "type": "ai-title",
            "aiTitle": f"title {uuid}",
            "timestamp": when,
            "sessionId": uuid,
        },
        {
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "timestamp": when,
            "sessionId": uuid,
        },
    ]
    jsonl = (
        tmp_sessions_dir / ".claude" / "projects" / "proj-a" / f"{uuid}.jsonl"
    )
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


@pytest.fixture
def _two_crash_sessions(
    tmp_sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Strong word-match, semantically far from "segfault".
    _write_claude_body_session(
        tmp_sessions_dir,
        "sem-words",
        "crash crash crash crash marketing dashboard",
        "2026-06-20T10:00:00Z",
    )
    # Weaker word-match, semantically close (mentions segfault).
    _write_claude_body_session(
        tmp_sessions_dir,
        "sem-meaning",
        "crash once, then a segfault with a backtrace and a core dump here",
        "2026-01-01T10:00:00Z",
    )
    base = str(tmp_sessions_dir / ".claude" / "projects")
    monkeypatch.setattr(
        "ai_r.parsers.claude._resolve_base_dir", lambda bd=None: Path(base)
    )


def test_search_sessions_semantic_fallback_keeps_bm25_order(
    _two_crash_sessions: None,
) -> None:
    _force_unavailable()
    result = search_sessions("crash", agent="claude", scope="body", sort="semantic")
    assert result["count"] == 2
    # Honest degradation: same order as plain BM25 relevance…
    bm25 = search_sessions("crash", agent="claude", scope="body", sort="relevance")
    assert [s["uuid"] for s in result["results"]] == [
        s["uuid"] for s in bm25["results"]
    ]
    # …and the response says why.
    sem = result["semantic"]
    assert sem["active"] is False
    assert sem["fallback"] == "bm25"
    assert "ai-r[semantic]" in sem["reason"]
    # The plain-BM25 response never carries the field.
    assert "semantic" not in bm25


def test_search_sessions_semantic_active_reranks_by_meaning(
    _two_crash_sessions: None,
) -> None:
    _install_fake_embedder()
    bm25 = search_sessions("crash", agent="claude", scope="body", sort="relevance")
    assert [s["uuid"] for s in bm25["results"]][0] == "sem-words"
    result = search_sessions("crash", agent="claude", scope="body", sort="semantic")
    assert [s["uuid"] for s in result["results"]][0] == "sem-meaning"
    sem = result["semantic"]
    assert sem["active"] is True
    assert sem["model"] == semantic.MODEL_NAME
    assert sem["candidates"] == 2
    assert sem["weight"] == semantic.SEMANTIC_WEIGHT


def test_search_sessions_semantic_zero_matches_reports_availability(
    _two_crash_sessions: None,
) -> None:
    _force_unavailable()
    result = search_sessions(
        "no-such-term-anywhere", agent="claude", scope="body", sort="semantic"
    )
    assert result["count"] == 0
    assert result["semantic"]["active"] is False
    assert "diagnostics" in result


def test_query_semantic_fallback_and_notice(
    _two_crash_sessions: None,
) -> None:
    _force_unavailable()
    result = mcp_query(text="crash", sort="semantic", agent="claude")
    assert result["count"] == 2
    sem = result["semantic"]
    assert sem["active"] is False and sem["fallback"] == "bm25"
    # Same order as the BM25 relevance sort (the honest fallback).
    relevance = mcp_query(text="crash", sort="relevance", agent="claude")
    assert [e["id"] for e in result["events"]] == [
        e["id"] for e in relevance["events"]
    ]
    assert "semantic" not in relevance


def test_query_semantic_active_reranks_events(
    _two_crash_sessions: None,
) -> None:
    _install_fake_embedder()
    result = mcp_query(text="crash", sort="semantic", agent="claude")
    assert result["count"] == 2
    assert result["events"][0]["session_id"] == "sem-meaning"
    assert result["semantic"]["active"] is True


def test_query_semantic_without_text_facet_orders_by_date(
    _two_crash_sessions: None,
) -> None:
    _force_unavailable()
    result = mcp_query(type="user_turn", sort="semantic", agent="claude")
    # No text facet -> date order (same contract as sort="relevance"),
    # semantic never attempted, no notice.
    assert result["count"] == 2
    ts = [e["ts"] for e in result["events"]]
    assert ts == sorted(ts)
    assert "semantic" not in result


def test_query_unknown_sort_names_semantic() -> None:
    result = mcp_query(text="x", sort="bogus")
    assert result["error"] == "invalid_argument"
    assert "semantic" in result["message"]


# ---------------------------------------------------------------------------
# Backward compat: without semantic anything, default surfaces are unchanged
# ---------------------------------------------------------------------------


def test_default_sorts_never_touch_semantic(
    _two_crash_sessions: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:  # pragma: no cover — must never run
        raise AssertionError("semantic probe ran on a non-semantic sort")

    monkeypatch.setattr(semantic, "_get_embedder", _boom)
    r1 = search_sessions("crash", agent="claude", scope="body")
    r2 = search_sessions("crash", agent="claude", scope="body", sort="date")
    r3 = mcp_query(text="crash", sort="relevance", agent="claude")
    for r in (r1, r2, r3):
        assert "semantic" not in r


# ---------------------------------------------------------------------------
# Resource limits: CPU-thread cap + idle release (long-lived MCP process)
# ---------------------------------------------------------------------------


def _write_fake_model(tmp_path: Path) -> Path:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model_qint8_avx512_vnni.onnx").write_bytes(b"onnx")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    return model_dir


def test_thread_cap_reaches_session_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The cap must actually land in the SessionOptions handed to onnxruntime,
    # not just be computed and dropped.
    fake_ort = _fake_runtime_modules(monkeypatch)
    monkeypatch.setattr(semantic.os, "cpu_count", lambda: 32)
    monkeypatch.setenv("AI_R_SEMANTIC_THREADS", "3")
    monkeypatch.setenv("AI_R_SEMANTIC_MODEL_DIR", str(_write_fake_model(tmp_path)))

    assert semantic_status()["available"] is True
    opts = fake_ort.InferenceSession.last_options
    assert opts is not None
    assert opts.intra_op_num_threads == 3
    assert opts.inter_op_num_threads == 3


def test_thread_cap_env_override_default_and_bad_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantic.os, "cpu_count", lambda: 16)
    # Default when the env is unset.
    monkeypatch.delenv("AI_R_SEMANTIC_THREADS", raising=False)
    assert _thread_cap() == semantic._DEFAULT_THREADS
    # Explicit override wins.
    monkeypatch.setenv("AI_R_SEMANTIC_THREADS", "6")
    assert _thread_cap() == 6
    # Blank / garbage / non-positive → default (never a crash).
    for bad in ("", "   ", "nope", "0", "-4", "3.5"):
        monkeypatch.setenv("AI_R_SEMANTIC_THREADS", bad)
        assert _thread_cap() == semantic._DEFAULT_THREADS


def test_thread_cap_never_exceeds_cpu_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Asking for more threads than cores clamps to the core count; a 1-core
    # box still gets at least 1.
    monkeypatch.setattr(semantic.os, "cpu_count", lambda: 2)
    monkeypatch.setenv("AI_R_SEMANTIC_THREADS", "99")
    assert _thread_cap() == 2
    monkeypatch.setattr(semantic.os, "cpu_count", lambda: None)  # unknown
    assert _thread_cap() == 1


def test_idle_predicate_is_pure_and_time_injected() -> None:
    # Not idle: used 10s ago, threshold 300s.
    assert _is_idle(now=1010.0, last_used=1000.0, idle_sec=300.0) is False
    # Idle: exactly at and past the threshold.
    assert _is_idle(now=1300.0, last_used=1000.0, idle_sec=300.0) is True
    assert _is_idle(now=1301.0, last_used=1000.0, idle_sec=300.0) is True
    # Nothing loaded → never idle (nothing to release).
    assert _is_idle(now=9999.0, last_used=None, idle_sec=1.0) is False


def test_idle_env_override_and_bad_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_R_SEMANTIC_IDLE_SEC", raising=False)
    assert semantic._idle_seconds() == semantic._DEFAULT_IDLE_SEC
    monkeypatch.setenv("AI_R_SEMANTIC_IDLE_SEC", "42")
    assert semantic._idle_seconds() == 42.0
    for bad in ("", "  ", "soon", "0", "-1"):
        monkeypatch.setenv("AI_R_SEMANTIC_IDLE_SEC", bad)
        assert semantic._idle_seconds() == semantic._DEFAULT_IDLE_SEC


def test_release_if_idle_keeps_busy_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_R_SEMANTIC_IDLE_SEC", "300")
    _install_fake_embedder()
    semantic._STATE["last_used"] = 1000.0
    # Only 10s elapsed → busy → not released.
    assert release_if_idle(now=1010.0) is False
    assert semantic._STATE["embedder"] is not None
    assert semantic._STATE["probed"] is True


def test_release_if_idle_frees_idle_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_R_SEMANTIC_IDLE_SEC", "300")
    _install_fake_embedder()
    semantic._STATE["last_used"] = 1000.0
    # 400s elapsed → idle → released back to the un-probed state.
    assert release_if_idle(now=1400.0) is True
    assert semantic._STATE["embedder"] is None
    assert semantic._STATE["probed"] is False
    assert semantic._STATE["last_used"] is None


def test_release_if_idle_noop_when_nothing_loaded() -> None:
    # An un-probed (or cached-unavailable) state has nothing to free.
    assert release_if_idle(now=1e9) is False
    _force_unavailable()
    assert release_if_idle(now=1e9) is False
    assert semantic._STATE["probed"] is True  # cached "missing" left intact


def test_reload_after_idle_release_through_fake_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After an idle release the next request re-probes and re-loads cleanly."""
    fake_ort = _fake_runtime_modules(monkeypatch)
    monkeypatch.setenv("AI_R_SEMANTIC_MODEL_DIR", str(_write_fake_model(tmp_path)))
    monkeypatch.setenv("AI_R_SEMANTIC_IDLE_SEC", "300")

    # First load.
    assert semantic_status()["available"] is True
    first = semantic._STATE["embedder"]
    assert isinstance(first, _Embedder)

    # Simulate a long quiet stretch, then release.
    semantic._STATE["last_used"] = 1000.0
    assert release_if_idle(now=1400.0) is True
    assert semantic._STATE["embedder"] is None

    # Next access re-probes and rebuilds a fresh embedder — no crash.
    status = semantic_status()
    assert status == {"available": True, "model": semantic.MODEL_NAME}
    second = semantic._STATE["embedder"]
    assert isinstance(second, _Embedder)
    assert second is not first
    assert semantic._STATE["last_used"] is not None


def test_get_embedder_releases_idle_on_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale embedder is dropped and rebuilt on the next _get_embedder call."""
    _fake_runtime_modules(monkeypatch)
    monkeypatch.setenv("AI_R_SEMANTIC_MODEL_DIR", str(_write_fake_model(tmp_path)))
    monkeypatch.setenv("AI_R_SEMANTIC_IDLE_SEC", "300")

    embedder, _ = semantic._get_embedder()
    first = embedder
    assert first is not None

    # Backdate last_used past the threshold; monotonic() > this so it's idle.
    semantic._STATE["last_used"] = time.monotonic() - 1000.0
    embedder2, reason2 = semantic._get_embedder()
    assert reason2 is None
    assert embedder2 is not None
    assert embedder2 is not first  # rebuilt, not the stale handle


def test_idle_release_preserves_honest_degradation() -> None:
    # Honest degradation is untouched by the resource machinery: no deps /
    # no model → (None, reason), never a crash, and release is a safe no-op.
    _force_unavailable()
    order, info = semantic_order("crash", ["a", "b"], [1.0, 2.0])
    assert order is None
    assert info["active"] is False
    assert info["fallback"] == "bm25"
    assert release_if_idle() is False  # nothing loaded to free


# ---------------------------------------------------------------------------
# Optional host smoke test: the real model, when actually installed
# ---------------------------------------------------------------------------


def _real_model_ready() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except Exception:
        return False
    import os

    real_dir = (
        Path(os.path.expanduser("~"))
        / ".cache" / "ai-r" / "semantic" / "multilingual-e5-small"
    )
    return (
        semantic._find_model_file(real_dir) is not None
        and (real_dir / "tokenizer.json").is_file()
    )


@pytest.mark.host
@pytest.mark.skipif(
    not _real_model_ready(),
    reason="ai-r[semantic] deps or model files not present on this host",
)
def test_real_model_cross_lingual_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    monkeypatch.setenv(
        "AI_R_SEMANTIC_MODEL_DIR",
        str(
            Path(os.path.expanduser("~"))
            / ".cache" / "ai-r" / "semantic" / "multilingual-e5-small"
        ),
    )
    docs = [
        "тесты упали с ошибкой сегментации",   # ru, on-topic
        "quarterly marketing budget review",     # en, off-topic
    ]
    order, info = semantic_order("the test suite segfaulted", docs, [1.0, 1.0])
    assert info["active"] is True
    assert order is not None
    assert order[0] == 0, "cross-lingual ru<->en match must outrank the off-topic doc"
