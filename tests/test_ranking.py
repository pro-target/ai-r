"""Unit tests for the pure-stdlib BM25 ranking helpers.

Covers :func:`ai_r.ranking.tokenize` and :func:`ai_r.ranking.bm25_scores`.
The properties under test are the ones :func:`search_sessions` relies on:
term-frequency monotonicity, rare-term weighting, length normalisation,
and the empty-query / empty-corpus zero contract.
"""
from __future__ import annotations

from ai_r.ranking import bm25_scores, tokenize


# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------


def test_tokenize_splits_on_non_alnum() -> None:
    assert tokenize("foo bar-baz") == ["foo", "bar", "baz"]


def test_tokenize_keeps_digits() -> None:
    assert tokenize("pwa2 manifest42") == ["pwa2", "manifest42"]


def test_tokenize_empty_is_empty() -> None:
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_tokenize_assumes_lowercased_input() -> None:
    # The regex is [a-z0-9]+ only — uppercase is treated as a separator,
    # which is fine because callers pass already-lowercased text.
    assert tokenize("Foo") == ["oo"]


def test_tokenize_drops_punctuation_runs() -> None:
    assert tokenize("...needle???") == ["needle"]


# ---------------------------------------------------------------------------
# bm25_scores: degenerate inputs
# ---------------------------------------------------------------------------


def test_bm25_empty_query_returns_zeros() -> None:
    docs = [tokenize("a b c"), tokenize("d e f")]
    assert bm25_scores([], docs) == [0.0, 0.0]


def test_bm25_empty_corpus_returns_empty() -> None:
    assert bm25_scores(tokenize("anything"), []) == []


def test_bm25_no_matching_term_scores_zero() -> None:
    docs = [tokenize("alpha beta"), tokenize("gamma delta")]
    scores = bm25_scores(tokenize("missing"), docs)
    assert scores == [0.0, 0.0]


def test_bm25_length_matches_doc_count() -> None:
    docs = [tokenize("x"), tokenize("y"), tokenize("z")]
    assert len(bm25_scores(tokenize("x"), docs)) == 3


# ---------------------------------------------------------------------------
# bm25_scores: core ranking properties
# ---------------------------------------------------------------------------


def test_bm25_term_frequency_monotonic() -> None:
    """More occurrences of the query term ⇒ strictly higher score
    (lengths held equal with filler so only ``f`` differs)."""
    one = tokenize("needle pad pad pad")
    two = tokenize("needle needle pad pad")
    scores = bm25_scores(tokenize("needle"), [one, two])
    assert scores[1] > scores[0] > 0.0


def test_bm25_rare_term_outranks_common_term() -> None:
    """A term appearing in few docs has a higher IDF, so a doc matching
    only the rare term outscores a doc matching only the common term."""
    # 'common' appears in every doc; 'rare' in just one.
    docs = [
        tokenize("rare common"),    # doc 0: matches rare + common
        tokenize("common filler"),  # doc 1: matches common only
        tokenize("common filler"),  # doc 2: matches common only
        tokenize("common filler"),  # doc 3: matches common only
    ]
    scores = bm25_scores(tokenize("rare common"), docs)
    # doc 0 (has the rare term) must rank strictly above the common-only docs.
    assert scores[0] > scores[1]
    assert scores[1] == scores[2] == scores[3]


def test_bm25_rare_alone_beats_common_alone() -> None:
    """Isolate IDF: doc with one rare-term hit beats a same-length doc
    with one common-term hit."""
    docs = [
        tokenize("rare x"),      # doc 0: the rare term (+ filler)
        tokenize("common x"),    # doc 1: the common term (+ filler)
        tokenize("common y"),    # doc 2: pads 'common' frequency up
        tokenize("common z"),    # doc 3: pads 'common' frequency up
    ]
    scores = bm25_scores(tokenize("rare common"), docs)
    assert scores[0] > scores[1]


def test_bm25_length_normalization_prefers_shorter_doc() -> None:
    """Same term frequency, but the shorter document scores higher
    because of the |d|/avgdl length-normalisation penalty."""
    short = tokenize("needle short")
    long = tokenize("needle " + " ".join(f"w{i}" for i in range(40)))
    scores = bm25_scores(tokenize("needle"), [short, long])
    assert scores[0] > scores[1] > 0.0


def test_bm25_idf_non_negative_even_when_in_all_docs() -> None:
    """The ``+1`` inside the IDF keeps weights non-negative even when a
    term appears in every document."""
    docs = [tokenize("ubiquitous a"), tokenize("ubiquitous b")]
    scores = bm25_scores(tokenize("ubiquitous"), docs)
    assert all(s >= 0.0 for s in scores)
    # Both docs are equal length and equal frequency ⇒ equal score.
    assert scores[0] == scores[1]
