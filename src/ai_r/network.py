"""The ``network`` preset (F4.3) — network-egress audit across agents.

Answers "where did an agent reach out to the network — and how risky did
those requests look?" in one call.  A *network event* is any tool call the
wrapper-aware classifier marks ``tool_kind="web"`` (F3.1): Claude
``WebFetch``/``WebSearch``, OpenCode ``webfetch``, Codex ``web_search``
(surfaced from ``web_search_call`` rollout records), Gemini/Antigravity
``web_fetch``/``google_web_search``.  Pi records no web tool — honest
absence, nothing fabricated.

This is a preset over the existing core, NOT a second engine (project
preset rule):

1. **Step 1 — candidates** come from ONE :func:`ai_r.events.query` scan
   (``type="tool_call"``, ``tool_kind="web"``) — session iteration,
   agent/session/date/noise/project_dir facets and event ids are all the
   query core's, nothing is re-implemented here.
2. **Deterministic selection** — the request target (``url``/``query``) is
   extracted from each call's own input and assessed with the **risk
   dictionary** (:data:`RISK_LABELS`): plain-HTTP scheme, credentials in
   the URL, a secret in the URL or in the search-query text (the F2.1
   redaction patterns double as the detector — one vocabulary, two uses),
   raw-IP host, private/local destination, punycode host.  Zero LLM, zero
   guessing: nothing extractable → honest ``null`` fields, empty
   ``risks``; a risk fires only on its regex/parse evidence.
3. **Token budget** — emitted ``url``/``query`` strings are char-capped,
   ``limit`` bounds the record count, and full context stays on-demand
   (the record's ``id`` is a query event id: walk neighbours via
   ``query(relative_to=...)`` or read the session).

Signal provenance (calibrated on this host's real corpus, 2026-07-05):
Claude ``WebFetch`` carries ``{url, prompt}`` and ``WebSearch``
``{query}``; OpenCode ``webfetch`` carries ``{url, format}``; Codex
``web_search_call`` records carry an ``action`` object (``search`` →
``query``, ``open_page``/``find_in_page`` → ``url``); Gemini-CLI (the
Antigravity family) declares ``web_fetch`` (URL embedded in a ``prompt``
string) and ``google_web_search`` (``query``) — verified against the
vendored reference source.  When a wrapper input carries no ``url`` key,
the first ``http(s)://`` URL embedded in its ``prompt`` text is used
(the Gemini ``web_fetch`` shape); further URLs in the same prompt are not
expanded into extra records — documented single-target simplification.

Honesty rules (same as the rest of the package): all agents are equal —
any parser that surfaces web-kind calls participates; a format without a
per-result error flag keeps ``is_error: null``; MCP-mediated network
access (``tool_kind="mcp"``, e.g. browser-automation servers) is NOT
classified as ``web`` — a name alone cannot prove an MCP server touches
the network, so those calls stay visible under ``tool_kind="mcp"``
instead of being guessed into this audit.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ai_r.events._common import resolve_tool
from ai_r.events.query import query as _query
from ai_r.parsers import PARSERS, Message, target_agents
from ai_r.redact import merge_redaction_counts, redact_text
from ai_r.security import coerce_tool_input as _coerce_input

__all__ = [
    "KIND_VALUES",
    "RISK_LABELS",
    "RISK_MODES",
    "assess_request",
    "network",
    "request_fields",
]


# --- vocabulary ---------------------------------------------------------------

# The request-kind vocabulary, derived from the extracted fields (never from
# the tool name alone): a ``url`` → ``fetch``, else a ``query`` → ``search``,
# else ``null`` (honest — the input carried no recognisable target).
KIND_VALUES: frozenset[str] = frozenset({"fetch", "search"})

# ``risk`` filter vocabulary (mirror of the incidents ``confirmed`` modes):
# ``include`` = all requests (default), ``only`` = requests with ≥1 risk,
# ``exclude`` = requests with no risk.
RISK_MODES: frozenset[str] = frozenset({"include", "only", "exclude"})

# The complete risk vocabulary, in emission order.  Deterministic dictionary,
# not a threat oracle: each label fires only on parse/regex evidence.
RISK_LABELS: Tuple[str, ...] = (
    "plain_http",             # scheme is http:// — cleartext transport
    "credentials_in_url",     # user:pass@ userinfo in the authority
    "secret_in_url",          # an F2.1 redaction pattern fires on the URL
    "secret_in_query",        # …or on the search-query text (exfil shape)
    "ip_literal_host",        # host is a raw IP (no DNS name to audit)
    "private_or_local_host",  # loopback/private/link-local/.local/.internal
    "punycode_host",          # xn-- label (homograph-capable name)
)

# Input keys that carry the request target, by preference (calibrated —
# see module docstring).
_URL_KEYS = ("url",)
_QUERY_KEYS = ("query",)
_PROMPT_KEYS = ("prompt",)

# First http(s) URL embedded in free text (the Gemini ``web_fetch`` prompt
# shape).  Conservative charset: stop at whitespace/quotes/brackets.
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\)\]]+", re.IGNORECASE)

# Hostname suffixes that denote a non-public destination.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")

# Emitted-string caps (chars) — the preset's token budget.  Full context
# stays on-demand via the event id.
_URL_CHARS_CAP = 500
_QUERY_CHARS_CAP = 240

_DEFAULT_LIMIT = 50


# --- extraction + assessment (exposed for tests) -----------------------------


def _first_str_value(payload: object, keys: Sequence[str]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def request_fields(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    """Extract ``(url, query)`` from a (coerced) web-tool input.

    ``url`` comes from an explicit ``url`` key, else from the first
    ``http(s)://`` URL embedded in a ``prompt`` string (Gemini
    ``web_fetch``), else — for a bare string payload — from the first URL
    in the string itself.  ``query`` comes from an explicit ``query`` key.
    Both are ``None`` when nothing extractable exists (honest, never
    guessed from the tool name).
    """
    url = _first_str_value(payload, _URL_KEYS)
    query = _first_str_value(payload, _QUERY_KEYS)
    if url is None:
        prompt = _first_str_value(payload, _PROMPT_KEYS)
        if prompt is None and isinstance(payload, str):
            prompt = payload
        if prompt:
            m = _URL_IN_TEXT_RE.search(prompt)
            if m:
                url = m.group(0)
    return url, query


def _host_of(url: str) -> Optional[str]:
    """The lowercased hostname of ``url``, or ``None`` when unparsable."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host or None


def assess_request(
    url: Optional[str], query: Optional[str] = None
) -> List[str]:
    """Risk labels firing on a request — a subset of :data:`RISK_LABELS`.

    Purely deterministic: URL structure via :func:`urllib.parse.urlsplit` +
    :mod:`ipaddress`, secret detection via the F2.1 redaction patterns
    (run on the RAW strings — the same vocabulary that later masks the
    emitted fields).  An unparsable URL contributes no URL-shape risks
    (never guessed); the secret scan still runs on the raw string.
    """
    risks: set[str] = set()
    if isinstance(url, str) and url:
        try:
            split = urlsplit(url)
        except ValueError:
            split = None
        if split is not None:
            if split.scheme.lower() == "http":
                risks.add("plain_http")
            try:
                userinfo = split.username
            except ValueError:  # pragma: no cover — malformed authority
                userinfo = None
            if userinfo is not None:
                risks.add("credentials_in_url")
            host = None
            try:
                host = split.hostname
            except ValueError:  # pragma: no cover — malformed authority
                host = None
            if host:
                try:
                    ip = ipaddress.ip_address(host)
                except ValueError:
                    ip = None
                if ip is not None:
                    risks.add("ip_literal_host")
                    if (
                        ip.is_private
                        or ip.is_loopback
                        or ip.is_link_local
                        or ip.is_unspecified
                    ):
                        risks.add("private_or_local_host")
                else:
                    lowered = host.lower()
                    if lowered == "localhost" or lowered.endswith(
                        _LOCAL_SUFFIXES
                    ):
                        risks.add("private_or_local_host")
                    if any(
                        label.startswith("xn--")
                        for label in lowered.split(".")
                    ):
                        risks.add("punycode_host")
        _, counts = redact_text(url)
        if counts:
            risks.add("secret_in_url")
    if isinstance(query, str) and query:
        _, counts = redact_text(query)
        if counts:
            risks.add("secret_in_query")
    return [label for label in RISK_LABELS if label in risks]


# --- helpers ------------------------------------------------------------------


def _cap(text: str, cap: int) -> Tuple[str, bool]:
    """Head-cap ``text`` at ``cap`` chars; a cut edge is marked with ``…``."""
    if len(text) <= cap:
        return text, False
    return text[:cap] + "…", True


def _ref_value(refs: Sequence[dict], key: str) -> Optional[Any]:
    for r in refs or ():
        if isinstance(r, dict) and key in r:
            return r[key]
    return None


def _web_entries(msg: Any) -> List[dict]:
    """The message's ``web``-kind tool_use entries, in stream order.

    Mirrors the event-construction filter of
    :func:`ai_r.events.model._messages_to_events` exactly (dict entries
    with a non-empty string name, classified by :func:`resolve_tool`), so
    the k-th entry here corresponds to the k-th ``tool_kind="web"`` event
    of the same ``message_index``.
    """
    out: List[dict] = []
    for tool in getattr(msg, "tool_use", ()) or ():
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", "")
        if not isinstance(name, str) or not name:
            continue
        if resolve_tool(name, None)[0] == "web":
            out.append(tool)
    return out


# --- the preset ---------------------------------------------------------------


def network(
    *,
    agent: Optional[str] = None,
    session: Optional[Any] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    kind: Optional[str] = None,
    risk: str = "include",
    domain: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    noise: str = "include",
    project_dir: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Audit network egress — web-kind tool calls + risk heuristics (F4.3).

    The baked chain (see module docstring): ONE ``query`` scan for
    ``web``-kind tool calls → target extraction (``url``/``query``) from
    each call's own input → the deterministic risk dictionary → per-domain
    and per-risk rollups.

    Args:
        agent: Optional agent filter (``claude``/``codex``/...); ``None``
            = all agents (every parser that surfaces web calls
            participates; Pi records none — honest absence).
        session: Optional session scope — a single uuid or a list of uuids
            (same semantics/validation as the ``query`` facet).
        since / until: ISO-8601 bounds (inclusive) on the call timestamp.
        kind: Optional request-kind filter — ``"fetch"`` (a URL target)
            or ``"search"`` (a query target).  Derived from the extracted
            fields, never from the tool name.  Unknown values fail loud.
        risk: Risk filter — ``"include"`` (default: every request),
            ``"only"`` (requests with ≥1 risk label), ``"exclude"``
            (requests with none).
        domain: Optional destination filter — keeps requests whose host
            equals this domain or is a subdomain of it
            (``github.com`` matches ``api.github.com``).  Requests
            without a URL never match.
        limit: Max request records returned (``0`` = no cap, default
            ``50``).  ``count``/``risky_count``/``by_domain``/``by_risk``
            always reflect the FULL match set.
        noise / project_dir: Session-level filters, forwarded verbatim to
            the ``query`` scan (subagent noise, project scoping).
        redact: ``True`` (default) masks secrets in the emitted
            ``session_title``/``url``/``query`` fields as
            ``[REDACTED_<TYPE>]`` and adds a ``redactions`` type→count
            dict when anything was masked; ``False`` returns raw.  Risk
            assessment always runs on the RAW stored strings.  The char
            cap is applied AFTER redacting the full string (same order as
            ``query``/``incidents``), so a secret sliced by the cap edge
            can never leak partially.

    Returns:
        A dict::

            {
              "requests": [
                {
                  "id": "<session>:<seq>",     # query event id (context
                                               # on-demand via relative_to)
                  "agent", "session_id", "session_title", "ts",
                  "message_index": int,
                  "tool": "<raw tool name>",
                  "kind": "fetch" | "search" | null,   # null = no target
                  "url": "<capped>" | null,
                  "url_truncated": true,               # only when cut
                  "query": "<capped>" | null,
                  "query_truncated": true,             # only when cut
                  "domain": "<host>" | null,
                  "risks": ["plain_http", ...],        # possibly empty
                  "is_error": true | false | null   # null = no correlated
                                                    # outcome signal (honest)
                }, ...
              ],
              "count": N,               # full match set (post filters)
              "risky_count": M,         # records with >= 1 risk label
              "by_domain": {"api.github.com": 3, ...},  # url-less skipped
              "by_risk": {"plain_http": 2, ...},
              "truncated": bool,        # limit tripped
              "redactions": {...},      # only when something was masked
              "diagnostics": {...}      # only when count == 0
            }

        Records are ordered chronologically (ts ascending, undated last).

    Raises:
        ValueError: on invalid arguments (unknown ``kind``/``risk``/
            ``agent``/``noise``, malformed ``session``/``since``/``until``,
            empty ``domain``, negative ``limit``, non-bool ``redact``).
    """
    if kind is not None and kind not in KIND_VALUES:
        raise ValueError(
            f"kind must be one of {sorted(KIND_VALUES)}, got {kind!r}"
        )
    if risk not in RISK_MODES:
        raise ValueError(
            f"risk must be one of {sorted(RISK_MODES)}, got {risk!r}"
        )
    if domain is not None and (
        not isinstance(domain, str) or not domain.strip()
    ):
        raise ValueError(
            f"domain must be a non-empty hostname string, got {domain!r}"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError(
            f"limit must be a non-negative integer, got {limit!r}"
        )
    if not isinstance(redact, bool):
        raise ValueError(f"redact must be a bool, got {redact!r}")
    wanted_domain = domain.strip().lower().lstrip(".") if domain else None

    # --- Step 1: candidates from ONE query scan --------------------------
    # redact=False: internal call — target extraction / risk assessment
    # must see the RAW input; emission-time redaction below covers
    # everything we output.
    scanned_sessions: dict[str, Any] = {}
    events = _query(
        type="tool_call",
        tool_kind="web",
        agent=agent,
        session=session,
        since=since,
        until=until,
        limit=0,
        noise=noise,
        project_dir=project_dir,
        scanned_sessions_out=scanned_sessions,
        redact=False,
    )

    # Session title map, reused from the scan (no second corpus walk).
    title_by_uuid: dict[str, Optional[str]] = {}
    for sessions in scanned_sessions.values():
        for sess in sessions or ():
            title_by_uuid[sess.uuid] = getattr(sess, "title", None)

    parser_by_agent = {
        name.value.lower(): PARSERS[name] for name in target_agents(None)
    }

    # Group candidate events by session, then by message_index, so each
    # session's messages are read ONCE and paired with its events.
    by_session: dict[str, dict[int, List[dict[str, Any]]]] = {}
    agent_by_session: dict[str, str] = {}
    for ev in events:
        sid = ev.get("session_id") or ""
        idx = ev.get("message_index", -1)
        if not sid or not isinstance(idx, int) or idx < 0:
            continue
        by_session.setdefault(sid, {}).setdefault(idx, []).append(ev)
        agent_by_session[sid] = ev.get("agent") or ""

    records: List[dict[str, Any]] = []
    for sid, by_msg in by_session.items():
        parser = parser_by_agent.get(agent_by_session.get(sid, ""))
        if parser is None:  # pragma: no cover — agents come from the scan
            continue
        try:
            messages: Sequence[Message] = parser.read_messages(sid)
        except (FileNotFoundError, ValueError, OSError):
            continue
        for idx, msg_events in by_msg.items():
            if not (0 <= idx < len(messages)):
                continue
            entries = _web_entries(messages[idx])
            # Pair the k-th web event of this message with the k-th web
            # tool_use entry — both lists are built with the same filter in
            # the same order (see _web_entries).
            msg_events.sort(
                key=lambda e: int(str(e.get("id", "")).rsplit(":", 1)[-1] or 0)
            )
            for ev, entry in zip(msg_events, entries):
                payload = _coerce_input(entry.get("input", ""))
                url, query_text = request_fields(payload)
                req_kind = (
                    "fetch" if url else ("search" if query_text else None)
                )
                if kind is not None and req_kind != kind:
                    continue
                host = _host_of(url) if url else None
                if wanted_domain is not None:
                    if host is None:
                        continue
                    lowered = host.lower()
                    if lowered != wanted_domain and not lowered.endswith(
                        "." + wanted_domain
                    ):
                        continue
                risks = assess_request(url, query_text)
                if risk == "only" and not risks:
                    continue
                if risk == "exclude" and risks:
                    continue
                records.append({
                    "id": ev.get("id"),
                    "agent": ev.get("agent"),
                    "session_id": sid,
                    "session_title": title_by_uuid.get(sid),
                    "ts": ev.get("ts"),
                    "message_index": idx,
                    "tool": _ref_value(ev.get("refs") or (), "tool")
                    or ev.get("text"),
                    "kind": req_kind,
                    # Placeholders — the emitted strings are produced at
                    # emission time below (redact the FULL string first,
                    # THEN cap), only for records that survive the limit
                    # slice.
                    "url": None,
                    "query": None,
                    "domain": host,
                    "risks": risks,
                    # Honest tri-state: absent ref = no correlated outcome
                    # signal for this agent/call → null, never False.
                    "is_error": _ref_value(ev.get("refs") or (), "is_error"),
                    "_raw_url": url,
                    "_raw_query": query_text,
                })

    # Chronological order (ts ascending, undated last) — deterministic.
    records.sort(key=lambda r: (r["ts"] is None, r["ts"] or "", r["id"] or ""))

    total = len(records)
    risky_count = sum(1 for r in records if r["risks"])
    by_domain: Dict[str, int] = {}
    by_risk: Dict[str, int] = {}
    for r in records:
        if r["domain"]:
            by_domain[r["domain"]] = by_domain.get(r["domain"], 0) + 1
        for label in r["risks"]:
            by_risk[label] = by_risk.get(label, 0) + 1

    truncated = False
    if limit and total > limit:
        records = records[:limit]
        truncated = True

    # Emission-time redaction + capping (F2.1): only records that survived
    # the limit slice pay for it; extraction/assessment above ran on the
    # RAW stored strings.  ORDER MATTERS (same rule as ``query``/
    # ``incidents``): the FULL raw string is redacted first, THEN capped —
    # a secret sliced by the cap edge can therefore never leak partially.
    redactions: dict[str, int] = {}
    for r in records:
        raw_url = r.pop("_raw_url")
        raw_query = r.pop("_raw_query")
        for field, raw, cap in (
            ("url", raw_url, _URL_CHARS_CAP),
            ("query", raw_query, _QUERY_CHARS_CAP),
        ):
            if raw is None:
                continue
            emit = raw
            if redact:
                emit, counts = redact_text(raw)
                if counts:
                    merge_redaction_counts(redactions, counts)
            capped, cut = _cap(emit, cap)
            r[field] = capped
            if cut:
                r[f"{field}_truncated"] = True
        if redact:
            new_val, counts = redact_text(r.get("session_title"))
            if counts:
                r["session_title"] = new_val
                merge_redaction_counts(redactions, counts)

    response: dict[str, Any] = {
        "requests": records,
        "count": total,
        "risky_count": risky_count,
        "by_domain": by_domain,
        "by_risk": by_risk,
        "truncated": truncated,
    }
    if redactions:
        response["redactions"] = redactions
    if total == 0:
        # Zero requests: attach corpus diagnostics (missing source dir vs
        # all-excluding filter vs a genuinely offline history).  Lazy import
        # mirrors find_tool_calls / incidents.
        from ai_r.diagnostics import empty_result_diagnostics

        response["diagnostics"] = empty_result_diagnostics(
            agent=agent,
            since=since,
            until=until,
            filters={
                "session": session,
                "kind": kind,
                # defaults are never the cause of emptiness — echo only
                # the non-default values.
                "risk": None if risk == "include" else risk,
                "domain": domain,
                "noise": None if noise == "include" else noise,
                "project_dir": project_dir,
            },
            scanned_sessions=scanned_sessions,
            redact_active=redact,
        )
    return response
