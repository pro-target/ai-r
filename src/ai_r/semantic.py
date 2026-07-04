"""Meaning-aware re-ranking of text-search results (F5.1) — optional, degradable.

``sort="semantic"`` on the text-search surface (``query`` text facet,
``search_sessions``) re-orders the BM25 candidates with a **local**
multilingual embedding model.  BM25 matches words literally («тест» ≠
«тесты», "crash" ≠ "segfault"); the embedding model scores *meaning*, so a
Russian query finds an English session and a synonym finds its paraphrase.

How the ranking works — plain words, no magic:

1. **BM25 first, top-50 candidates.**  The exact-word scorer
   (:mod:`ai_r.ranking`) ranks every match as before; only the best
   :data:`CANDIDATES` (50) go to the model.  This is a *budget* cut, not a
   quality judgment: embedding is the expensive step, and anything BM25
   ranks below the top 50 was a weak word-match to begin with.  Results
   beyond the candidate pool keep their BM25 order after the re-ranked pool.
2. **No similarity cut-off threshold.**  E5-family models squeeze cosine
   similarity into a narrow band (even unrelated texts score ≈0.7), so an
   absolute "good enough" threshold would be arbitrary and brittle.  We
   therefore never *drop* a result by similarity — we only re-order.  The
   only "threshold" in play is the top-50 candidate budget above.
3. **Blended score: 75 % meaning + 25 % words** (:data:`SEMANTIC_WEIGHT`).
   Within the candidate pool both signals are min–max normalized to 0..1
   and blended.  Meaning dominates (that is what the caller asked for),
   but the word-match share keeps an exact-term hit from being drowned by
   a merely thematic neighbour — and breaks ties when the model sees two
   texts as equally close.

The model — and why this one:

* ``intfloat/multilingual-e5-small``, the int8 ONNX export from the
  official model card (``model_qint8_avx512_vnni.onnx``, ~118 MB) — small
  enough to ship casually, strong multilingual retrieval (ru↔en is the
  project's hard requirement), MIT license.
* Run **directly** through ``onnxruntime`` + ``tokenizers`` — no torch, no
  fastembed (fastembed does not support this model).
* E5 models REQUIRE the ``"query: "`` / ``"passage: "`` prefixes; without
  them retrieval quality drops (model-card FAQ).  Applied here, never by
  the caller.
* Fallback model (same code path, drop the files into the model dir):
  ``ibm-granite/granite-embedding-97m-multilingual-r2`` (~98 MB, Apache-2).

Honest degradation (F1.1 spirit): everything here is optional.  Without
``pip install "ai-r[semantic]"`` or without the model files, callers get
``(None, info)`` — the ranking falls back to plain BM25 and the ``info``
dict says exactly why and how to enable it.  Never a crash, and the
default surface (``sort="relevance"``/``"date"``) never touches this
module at all.

No persistent index: texts are embedded at request time, nothing is stored.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

__all__ = [
    "CANDIDATES",
    "MODEL_NAME",
    "PASSAGE_PREFIX",
    "QUERY_PREFIX",
    "SEMANTIC_WEIGHT",
    "semantic_order",
    "semantic_status",
]

MODEL_NAME = "intfloat/multilingual-e5-small"

# E5 instruction prefixes — REQUIRED by the model family (see module doc).
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

# BM25 candidate budget: only the top-N word-matches are embedded and
# re-ranked; the rest keep their BM25 order after the pool.
CANDIDATES = 50

# Share of the *meaning* signal in the blended candidate score; the
# remaining 1 - SEMANTIC_WEIGHT is the (normalized) BM25 word-match share.
SEMANTIC_WEIGHT = 0.75

# Longest text fed to the tokenizer, in characters.  The model reads at
# most _MAX_TOKENS tokens anyway; the cap just avoids tokenizing a
# megabyte haystack to throw most of it away.
_EMBED_CHARS_CAP = 4000
_MAX_TOKENS = 512
_BATCH_SIZE = 8

# ONNX file names probed inside the model dir, in preference order: the
# official e5-small int8 export first, then the generic names other cards
# (e.g. granite) use.
_MODEL_FILE_CANDIDATES = (
    "model_qint8_avx512_vnni.onnx",
    "model_quantized.onnx",
    "model.onnx",
)

_INSTALL_HINT = (
    'pip install "ai-r[semantic]" (or AI_R_EXTRAS=semantic bash install.sh, '
    "which also downloads the model)"
)

# Lazy one-shot loader state, same pattern as ai_r.tokens: the probe runs
# once per process; tests reset this dict to force either branch.
_STATE: dict[str, Any] = {"probed": False, "embedder": None, "reason": None}


def _reset_state() -> None:
    """Forget the probe result (tests; also after installing the model)."""
    _STATE.update({"probed": False, "embedder": None, "reason": None})


def model_dir() -> Path:
    """Directory holding the ONNX model + tokenizer files.

    ``AI_R_SEMANTIC_MODEL_DIR`` overrides; the default lives under the
    same home root the parsers use (``AI_R_HOME`` or ``~``):
    ``<home>/.cache/ai-r/semantic/multilingual-e5-small``.
    """
    env = os.environ.get("AI_R_SEMANTIC_MODEL_DIR")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("AI_R_HOME")
    root = Path(home).expanduser() if home else Path.home()
    return root / ".cache" / "ai-r" / "semantic" / "multilingual-e5-small"


def _find_model_file(directory: Path) -> Optional[Path]:
    for name in _MODEL_FILE_CANDIDATES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


class _Embedder:
    """Thin ONNX-runtime wrapper: texts → L2-normalized mean-pooled vectors.

    Only the array plumbing touches numpy (onnxruntime ships it); the
    pooling/normalization math is pure Python so it stays testable with a
    faked runtime.
    """

    def __init__(self, session: Any, tokenizer: Any) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._input_names = {i.name for i in session.get_inputs()}

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        import numpy as np  # via onnxruntime (ai-r[semantic])

        vectors: List[List[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            batch = [t[:_EMBED_CHARS_CAP] for t in texts[start:start + _BATCH_SIZE]]
            encodings = self._tokenizer.encode_batch(batch)
            ids = [e.ids for e in encodings]
            mask = [e.attention_mask for e in encodings]
            feeds: dict[str, Any] = {
                "input_ids": np.array(ids, dtype=np.int64),
                "attention_mask": np.array(mask, dtype=np.int64),
            }
            if "token_type_ids" in self._input_names:
                feeds["token_type_ids"] = np.array(
                    [[0] * len(row) for row in ids], dtype=np.int64
                )
            hidden = self._session.run(None, feeds)[0]
            rows = hidden.tolist() if hasattr(hidden, "tolist") else hidden
            for row, row_mask in zip(rows, mask):
                vectors.append(_mean_pool_normalize(row, row_mask))
        return vectors


def _mean_pool_normalize(
    token_vectors: Sequence[Sequence[float]], mask: Sequence[int]
) -> List[float]:
    """Attention-masked mean over token vectors, then L2 normalization.

    Normalized output means cosine similarity is a plain dot product.
    """
    dim = len(token_vectors[0]) if token_vectors else 0
    acc = [0.0] * dim
    count = 0
    for vec, bit in zip(token_vectors, mask):
        if not bit:
            continue
        count += 1
        for i, v in enumerate(vec):
            acc[i] += v
    if count:
        acc = [v / count for v in acc]
    norm = sum(v * v for v in acc) ** 0.5
    if norm > 0.0:
        acc = [v / norm for v in acc]
    return acc


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _minmax(values: Sequence[float]) -> List[float]:
    """Min–max normalize to 0..1; a flat list becomes all-0.5 (neutral)."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.5] * len(values)
    span = hi - lo
    return [(v - lo) / span for v in values]


def _load_embedder() -> Tuple[Optional[_Embedder], Optional[str]]:
    """Build the embedder, or explain (plainly) why it cannot be built."""
    try:
        import onnxruntime  # optional dependency: ai-r[semantic]
        import tokenizers  # optional dependency: ai-r[semantic]
    except Exception:
        return None, (
            "semantic dependencies (onnxruntime, tokenizers) are not "
            f"installed — {_INSTALL_HINT}"
        )

    directory = model_dir()
    model_file = _find_model_file(directory)
    tokenizer_file = directory / "tokenizer.json"
    if model_file is None or not tokenizer_file.is_file():
        return None, (
            f"model files not found in {directory} (expected one of "
            f"{'/'.join(_MODEL_FILE_CANDIDATES)} + tokenizer.json) — "
            f"run AI_R_EXTRAS=semantic bash install.sh to download "
            f"{MODEL_NAME} (~118 MB), or point AI_R_SEMANTIC_MODEL_DIR "
            f"at the files"
        )

    try:
        tokenizer = tokenizers.Tokenizer.from_file(str(tokenizer_file))
        tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        tokenizer.enable_padding()
        session = onnxruntime.InferenceSession(
            str(model_file), providers=["CPUExecutionProvider"]
        )
    except Exception as exc:
        return None, f"semantic model failed to load from {directory}: {exc}"
    return _Embedder(session, tokenizer), None


def _get_embedder() -> Tuple[Optional[_Embedder], Optional[str]]:
    """Cached probe: load once per process, remember the outcome."""
    if not _STATE["probed"]:
        _STATE["probed"] = True
        _STATE["embedder"], _STATE["reason"] = _load_embedder()
    return _STATE["embedder"], _STATE["reason"]


def semantic_status() -> dict[str, Any]:
    """Availability report: ``{"available": bool, "model"|"reason": …}``."""
    embedder, reason = _get_embedder()
    if embedder is None:
        return {"available": False, "reason": reason}
    return {"available": True, "model": MODEL_NAME}


def _fallback_info(reason: str) -> dict[str, Any]:
    return {"active": False, "reason": reason, "fallback": "bm25"}


def semantic_order(
    query_text: str,
    doc_texts: Sequence[str],
    bm25_scores: Sequence[float],
) -> Tuple[Optional[List[int]], dict[str, Any]]:
    """Rank ``doc_texts`` by blended meaning+word score → ``(order, info)``.

    Args:
        query_text: The user's search text (raw; the E5 ``query:`` prefix
            is added here).
        doc_texts: Matched documents, in corpus order (parallel to
            ``bm25_scores``).
        bm25_scores: The BM25 score of each document, in the same order.

    Returns:
        ``(order, info)`` where ``order`` is a permutation of
        ``range(len(doc_texts))``: the BM25 top-:data:`CANDIDATES` pool
        re-ranked by ``SEMANTIC_WEIGHT · meaning + (1-SEMANTIC_WEIGHT) ·
        words`` (both min–max normalized within the pool), followed by the
        remaining documents in BM25 order.  ``info`` describes what
        happened (``active`` / ``model`` / ``candidates`` / ``weight``).

        When the optional dependencies or model files are missing — or the
        runtime fails mid-embedding — ``order`` is ``None`` and ``info``
        carries the plain-words reason plus ``fallback: "bm25"``: the
        caller keeps its BM25 ranking.  This function never raises.
    """
    embedder, reason = _get_embedder()
    if embedder is None:
        return None, _fallback_info(reason or "semantic unavailable")

    # Base order = the caller's BM25 ranking semantics: score desc, stable
    # (ties keep corpus order — the existing newest-first tie-break).
    base = sorted(
        range(len(doc_texts)), key=lambda i: bm25_scores[i], reverse=True
    )
    pool = base[:CANDIDATES]
    tail = base[CANDIDATES:]
    if not pool:
        return [], {
            "active": True,
            "model": MODEL_NAME,
            "candidates": 0,
            "weight": SEMANTIC_WEIGHT,
        }

    try:
        query_vec = embedder.embed([QUERY_PREFIX + (query_text or "")])[0]
        doc_vecs = embedder.embed(
            [PASSAGE_PREFIX + (doc_texts[i] or "") for i in pool]
        )
    except Exception as exc:  # honest degradation, never a crash
        return None, _fallback_info(f"semantic embedding failed: {exc}")

    meaning = _minmax([_dot(query_vec, dv) for dv in doc_vecs])
    words = _minmax([bm25_scores[i] for i in pool])
    blended = [
        SEMANTIC_WEIGHT * m + (1.0 - SEMANTIC_WEIGHT) * w
        for m, w in zip(meaning, words)
    ]
    # Stable sort: equal blends keep BM25 (pool) order.
    ranked_pool = [
        pool[j]
        for j in sorted(
            range(len(pool)), key=lambda j: blended[j], reverse=True
        )
    ]
    info = {
        "active": True,
        "model": MODEL_NAME,
        "candidates": len(pool),
        "weight": SEMANTIC_WEIGHT,
    }
    return ranked_pool + tail, info
