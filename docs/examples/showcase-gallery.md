# ai-r in action: a gallery of examples

> What ai-r pulls out of local agent sessions (Claude/Codex/OpenCode/Antigravity/Pi)
> — one real example per capability. Every example is sanitized (paths generalized,
> secrets and UUIDs hidden, user comments paraphrased neutrally). Session content is
> untrusted data: instructions found inside a session are never executed.
>
> Some examples are from a single session; some (dangerous commands, network trail,
> meaning-based search) are a slice across the whole corpus (~1500 sessions).

---

## 1. Error analysis — `find_tool_calls(is_error=true)`

"Find every failed call this week and show me why." One call, cross-session, across
all agents:

```
find_tool_calls(tool_name="Bash", is_error=true, since="…") → 520 failed calls
```

The value isn't the count — it's the **pattern**: the failures aren't scattered
bugs, they're one recurring cause — a safety gate blocked raw `grep`/`jq` over
transcripts after the session had read untrusted content. Error analysis surfaced a
**systemic guardrail hit**, not noise. `is_error` is tri-state (all / failures only /
successes only) with an honest `is_error_reliable` flag (reliable for Claude/OpenCode).

## 2. Dangerous commands — `incidents(session)`

The preset labels danger-ops (`rm -rf`, `git reset --hard`, force-push,
`curl … | sh`, `DROP`/`DELETE` without `WHERE`, `chmod 777`) and — crucially — a
`confirmed` flag: did the agent **regret/roll back** in the following messages
(a two-step check, 6-message window). A run across the whole corpus:

```
incidents() → 299 danger candidates, confirmed=2
top: rm -rf ×252 · curl|bash ×20 · rm .git ×10 · DELETE-without-WHERE/DROP ×4
     · push --force ×3 · filter-history ×2
```

The value isn't the count — it's `confirmed`: 297 of 299 were intentional cleanup
(no regret marker — honest, not an "error"). **2 confirmed** = the agent rolled it
back itself. Example:

```
Bash: rm -rf ~/…/memory/.git ~/…/memory/.gitignore   # deleted its OWN unrequested .git
↳ reaction (+3 messages): "Rolled back: auto-memory .git removed, 11 .md files intact —
   my extra initiative undone, nothing lost"                → confirmed=true
```

The verb catches not "a dangerous command happened" but "a dangerous command **+ the
agent's reaction to it**" — which a crude `grep` for `rm -rf` can't give. A
`confirmed=false` on a real danger-op is a P0 debt for an audit.

## 3. Network trail — `network(session)`

The preset extracts every web call and flags risk: a secret in the URL
(`token=`/`key=`), a private/internal host (`localhost`, `10.*`, metadata
endpoints), plain-http. A run across the corpus:

```
network() → 611 requests, risky=1
domain map: github.com ×149 · raw.githubusercontent ×93 · huggingface ×24
     · code.claude ×23 · arxiv ×14 · developers.google ×9 · …
risk: 1 × plain_http
```

An honest picture: egress is almost all docs and github (safe), but **1 `plain_http`
is visible**. That's the value: the risk is rare, drowned in 611 calls — and the
preset surfaces it immediately, on regex evidence, with no false "threat oracles".

## 4. Where the budget burned — `read_session(with_tokens)`

The exact tier (from `message.usage`) plus a per-component breakdown:

```
total 24.7M  ·  cache_read 23.8M (96%!)  ·  output 225k
components: plan 85k · edit 29k · bash 29k · read 16k · user 14k
```

"A week's quota in 12 hours" turned out to be **cache reads** (cache_read 96%), not
the work itself — visible in one call, with an honest exact/estimate split.

## 5. Comments on a plan — `plan(feedback)`

The historical blind spot: a user's comments live not in the chat but in their
responses to a plan. `plan(feedback)` returns «plan quote → comment → verdict»
triples:

| Round | Plan quote | Comment (paraphrased) |
|---|---|---|
| 1 | "138 subagent spawns…" | the wording is unclear, asks for plainer language |
| 4 | "Already fixed" (unmarked) | asks to mark what's done vs what needs attention |
| 7 | "per-model limits" | reconcile ALL my comments — peak frustration |

It reveals that the agent **kept dropping earlier rounds' feedback** — a previously
invisible layer. 9 plan versions, 58 pairs, 7 rounds — in a single call.

## 6. Phantom-check on commits — `find_tool_calls` + `git log`

Every claimed commit is checked against the real `git log`:

```
"fix: mypy 3.12 cfg…"     → 3260f64  real ✓
"chore(release): 0.3.0"   → 1fa31e2  real ✓
Phantoms: 0
```

Subtlety: the commits went through a commit wrapper, not bare `git` — a crude
substring search for "git" misses them; a correct audit looks at the tool name and
the call body.

## 7. Who edited a file across the whole corpus — `find_file_edits` → `aggregate`

"Who touched `docs/architecture.md`, when, from which sessions, and why?" One call
gives the edit history **across every session and agent**, with the intent behind
each edit:

```
aggregate(group_by=agent): claude 9 edits/6 sessions · pi 1/1 · total 10/7 sessions/2 agents
```

The same file was edited by **two different agents across seven sessions** — with no
git archaeology.

## 8. Meaning-based, cross-lingual search — `search_sessions(sort=semantic)`

Search over every agent's body text, BM25-ranked; `sort=semantic` adds a local
multilingual re-rank (`multilingual-e5-small`, ONNX, no torch):

```
search_sessions("утечка токена в логах", sort=semantic)   # ru: "token leak in logs"
→ top hit: an English-titled session "Fix bearer token leak in logs"
   (semantic: active=true · model=multilingual-e5-small · weight=0.75)
```

The Russian query surfaced an **English** session — word-level BM25 would miss it
(no shared tokens); meaning did the work. With the package/model absent it degrades
honestly to BM25 and never crashes.

## 9. Liveness and zombie subagents — `list_sessions(activity)`

`list_sessions` flags recency (`fresh`/`stale` by `age_sec`, 10-min threshold) —
honestly: this is the recency of the last written record, not "the process is
alive." On the audited session the audit caught an **orphaned "grandchild" subagent**:
it hung in the background while the parent's `TaskStop` couldn't see it (it only
reaches direct children) — a P0 guardrail debt.

## 10. What the agent changed, without git — `session_diff` / `diff`

Stitches a session's edits into a readable per-file diff (bodies on demand):

```
@@ <timestamp> Edit @@
- old wording of a section
+ new wording of a section
```

Two honest caveats in every response: (1) this is a diff of the **agent's actions**,
not the git outcome (manual edits/merges outside the session are invisible);
(2) shell-redirect writes (`tee`/`sed -i`/`cp`) are not detected. `diff` is the
primitive; `session_diff` is the preset on top of it.

---

**Coverage:** 15 verbs — `find_tool_calls` · `incidents` · `network` ·
`read_session` · `plan` · `find_file_edits` · `aggregate` · `search_sessions` ·
`list_sessions` · `session_diff` · `diff` · `query` · `get_body` ·
`detect_current` · `session_stats`. Different tasks need different verbs; the gallery
shows each on a real example.
