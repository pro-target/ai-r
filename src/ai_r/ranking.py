"""Pure-stdlib BM25 relevance ranking.

Zero third-party dependencies (no ``rank_bm25``): the implementation is a
direct transcription of the Okapi BM25 scoring function using only
``math``, ``re`` and :class:`collections.Counter`.

Used by :func:`ai_r.mcp_server.search_sessions` to order matched sessions
by relevance instead of recency.  Tokenisation assumes the input is
already lowercased (the search haystacks are), so we only split on
alphanumeric runs.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import List

__all__ = ["tokenize", "bm25_scores"]

# Standard Okapi BM25 free parameters.
_K1 = 1.5
_B = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> List[str]:
    """Split already-lowercased ``text`` into alphanumeric tokens.

    Runs of ``[a-z0-9]`` become tokens; everything else is a separator.
    Empty input yields an empty list.  No lowercasing is performed here —
    callers pass text that is already lowercased (titles/haystacks).
    """
    if not text:
        return []
    return _TOKEN_RE.findall(text)


def bm25_scores(
    query_tokens: List[str],
    docs_tokens: List[List[str]],
) -> List[float]:
    """Okapi BM25 score of every document against ``query_tokens``.

    Args:
        query_tokens: The (already tokenised) query terms.
        docs_tokens: One token list per document, in corpus order.

    Returns:
        A list of float scores, one per document, in the same order as
        ``docs_tokens``.  An empty query or an empty corpus yields all
        zeros.

    Scoring (k1=1.5, b=0.75)::

        IDF(t)   = ln((N - n + 0.5) / (n + 0.5) + 1)
        score(d) = Σ_t  IDF(t) · (f · (k1 + 1))
                              / (f + k1 · (1 - b + b · |d| / avgdl))

    where ``N`` is the document count, ``n`` the number of documents
    containing term ``t``, ``f`` the frequency of ``t`` in ``d`` and
    ``avgdl`` the average document length.  The ``+ 1`` inside the IDF
    keeps every weight non-negative even for terms present in most docs.
    """
    n_docs = len(docs_tokens)
    if not query_tokens or n_docs == 0:
        return [0.0] * n_docs

    doc_term_counts: List[Counter] = [Counter(doc) for doc in docs_tokens]
    doc_lengths: List[int] = [len(doc) for doc in docs_tokens]
    avgdl = sum(doc_lengths) / n_docs if n_docs else 0.0

    # Document frequency per *unique* query term.
    query_terms = set(query_tokens)
    doc_freq: dict[str, int] = {}
    for term in query_terms:
        doc_freq[term] = sum(1 for tc in doc_term_counts if term in tc)

    idf: dict[str, float] = {
        term: math.log((n_docs - n + 0.5) / (n + 0.5) + 1.0)
        for term, n in doc_freq.items()
    }

    scores: List[float] = []
    for tc, dl in zip(doc_term_counts, doc_lengths):
        score = 0.0
        denom_len = _K1 * (1.0 - _B + _B * (dl / avgdl if avgdl else 0.0))
        for term in query_terms:
            f = tc.get(term, 0)
            if not f:
                continue
            score += idf[term] * (f * (_K1 + 1.0)) / (f + denom_len)
        scores.append(score)
    return scores
