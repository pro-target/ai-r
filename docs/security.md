# Security: untrusted session content

`ai-r` is a **read-only** session reader. It has no write surface
and no access-control layer (see
[Architecture -> read-only](architecture.md)). The security concern
is not *who may read* a session -- it is *what the reader's caller does
with the content*.

## Threat model

Agent session logs contain arbitrary text: user prompts, fetched web
pages, tool inputs and outputs, file contents the agent read, and
output from other models. None of this is trusted.

A consumer that feeds session content into an LLM (an auditor, a
summarizer, an orchestrator that replays a session, a reviewer agent)
passes that untrusted text into a model's context. Session content can
and does contain instruction-shaped strings: attempts to override the
consuming model's instructions, tool-call JSON embedded in a fetched
web page, or another agent's reasoning that *describes* a dangerous
command.

A naive consumer treats these as instructions. The model may obey
content that originated inside a session log it was only asked to
*read*. This is prompt injection via session logs.

## This is not theoretical

During the audit that produced this note, a consumer agent that had
read an untrusted session via `read_session` had a Bash action gated
by a prompt-injection guard, which fired correctly. Session logs from
every supported agent (Claude, Codex, OpenCode, Antigravity, Pi)
routinely contain fetches, tool outputs, and cross-agent text -- all
untrusted.

## What `ai-r` does (and does not)

`ai-r` is the parser layer. It **does not** sanitize, classify,
or redact instruction-shaped content by default -- doing so would
silently destroy session fidelity, which defeats the purpose of a
reader. Session text is returned verbatim.

The boundary is deliberate: trust decisions belong to the **consumer**,
not the reader.

For consumers that feed session text into another LLM, `ai-r` exposes
`ai_r.security.sanitize_session_text()`. The helper is opt-in: it wraps
content in an explicit untrusted-data frame and can bound prompt size,
but it preserves the original text instead of trying to remove
instruction-shaped strings.

The MCP server also applies output-size guards. `read_session` caps the
projected MCP message list, and `search_sessions(scope="body"|"all")`
caps body-search messages and haystack bytes. These guards protect MCP
payload size; they are not a trust decision and do not make session
content safe to execute.

## What consumers must do

Treat every string returned by `read_session` / `read_messages` /
`search_sessions` as **untrusted data**, not as instructions:

1. **Frame as data.** Wrap session content so the consuming model
   understands it is a record to analyze, not a directive.
2. **Never auto-execute** a tool call, shell command, or file write
   that originates *inside* session content. Gate every side-effecting
   action behind human approval, and verify the action matches the
   consumer's own task -- not the session's text.
3. **Sandbox the consumer.** Run audit/replay agents with a
   prompt-injection action gate (a hook that blocks risky tools once
   untrusted content has entered context) and minimal filesystem /
   network permissions.
4. **Review cross-agent content twice.** Sessions that reference other
   sessions (via `find_file_edits`, quoted transcripts, or pasted logs)
   chain untrusted sources -- the injection surface compounds.

## `find_file_edits` intent extraction

`find_file_edits` returns an `intent` string mined from the session
that edited a file. That string is session-derived and therefore
untrusted by the same rule: display it, cite it, but do not act on it
as an instruction.

## `user_ref` targets are external pointers

A `user_turn` event carries `user_ref` entries — the files, urls, images
and IDE-context the user attached (see [Architecture -> user_ref
ADR](architecture.md#decisions) and `docs/methods.md` -> *User
references*). Each entry's `target` is a **pointer** (a path / url /
filename), not content: ai-r marks THAT the user attached something, it
does not fetch or sanitize what the pointer refers to.

This makes `target` a signpost to untrusted, and often *more* untrusted,
data. The `target` string itself is session-derived (redacted on
emission like any other text, but still untrusted as a value). The thing
it points at is worse: a URL is an arbitrary web page, a file path is
arbitrary file content — none of it was authored by the session and none
of it is trusted.

A consumer that follows a `target` — fetches the URL, reads the file —
MUST treat the retrieved bytes as untrusted data and wrap them through
`ai_r.security.sanitize_session_text()` before letting a model see them.
This is the compounding case of rule 4 above: the session pointer is one
untrusted hop, the content it resolves to is a second, freshly-fetched
untrusted hop. Never fetch-and-feed a `target` into a model's context
unframed, and never auto-execute anything derived from it.

## The http transport token

The optional shared http transport reads its bearer token from the
environment (`AI_R_HTTP_TOKEN`). Processes running as the **same OS
user** are outside this threat model: they can already read the session
files directly (and the server's environment, e.g.
`/proc/<pid>/environ`), so the token guards against other local users
on a shared box and -- for an explicit remote bind -- remote callers,
nothing more.

## Related

- [Architecture](architecture.md) -- read-only design, no access layer.
- The `ee72961` access-control removal decision (see
  [Architecture -> Decisions](architecture.md#decisions)):
  caller-authorization ("may caller read") was removed because ai-r is
  a local single-user tool and any local caller already has filesystem
  access to the session files; this threat model is the *orthogonal*
  concern -- *what the reader's caller does with content*.
