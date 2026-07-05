# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> `git` показывает, **что** попало в код. `ai-r` показывает, **почему**: какой
> агент это сделал, под каким планом — и не выронил ли он тихо тот план, на
> котором сам же остановился. Read-only, по всем пяти агентам для кода, один
> интерфейс.

Агент отчитался: «сделал X по плану Y». Проверить нечем. План — в одном
формате, правки — в другом. А если над задачей работали два агента, их истории
вообще не сводятся: каждый пишет по-своему и в своём месте. `ai-r` читает
историю сессий агента и достаёт из неё намерение, план и авторство за правкой.

## Быстрый пример — агент спрашивает историю

Главный режим — **MCP**: агент (Claude, Codex, …) зовёт `ai-r` напрямую и
спрашивает про историю обычным языком. Например — вытащить план, на котором
остановился прошлый агент, отбросив черновики:

```
Покажи план из прошлой сессии — только финальный, без промежуточных ревизий.
→ ai-r: plan(session=…, kind="final")  →  get_body(id, shallow=true)
        возвращает финальную задачу + список dropped_drafts
```

Быстрая атрибуция правок — одна команда в терминале, сразу по всем агентам:

```bash
# кто правил этот файл и когда — кросс-агентно, опционально за период
ai-r find-file-edits auth.py --since 2026-06-01
```

## Что болит

- «Готово, сделал X по плану Y» — а проверить нечем: план агент держит в одном
  виде, правки в другом.
- Сменили агента посреди задачи — потеряли нить. Спросить «что *другой* агент
  уже пробовал?» негде.
- Всплыла правка в файле — непонятно, **какой** агент её сделал и по какому
  запросу.

Причина одна: каждый агент пишет историю **по-своему** — Claude и Codex в
JSONL, OpenCode в SQLite, Antigravity в «brain»-директориях, Pi в JSONL по
проектам. Пять форматов, пять раскладок — вместе они не сводятся.

## Обещание

`ai-r` сводит все пять в **один read-only интерфейс**. Наведите любого агента —
или скрипт, или себя — на любую сессию, неважно, какой инструмент её записал.
Одна форма запроса для каждого агента; различия форматов нормализуются внутри
парсеров.

## Ключевые возможности

- **«Почему?», не только «Что?».** Извлекает план, намерение и авторство за
  правкой — не только текст диффа. `git diff` говорит, *что* изменилось;
  `ai-r` — под каким планом и по чьему запросу.
- **Финальный план, а не черновики.** `ai-r` достаёт план, на котором агент
  *остановился*, и отдельно показывает, что он по дороге выбросил
  (`dropped_drafts`) — по Claude / Codex / Antigravity, где сигналы плана у
  каждого свои.
- **Кросс-агентная атрибуция.** Любая правка файла или вызов инструмента →
  агент, что её сделал, плюс запустивший её запрос (`find-file-edits` /
  `find-tool-calls`).
- **Маленький ответ, тело по требованию.** Записи несут ссылку на содержимое
  (хэш + длину), а полный текст правки берётся отдельным запросом — ответ не
  раздувается.
- **Работает через MCP (13 инструментов).** Агент зовёт `ai-r` напрямую
  обычным языком; те же данные доступны из терминала (CLI) и из кода (Python
  SDK).
- **Читалка, не охранник.** Достаёт сущности; граф знаний и память строишь ты
  (или твой инструмент). Только чтение: ничего не запускает и не пишет в
  историю агента.

## Зачем это

- **Аудит сессий свежим взглядом.** Новый агент с пустым контекстом холодно
  проверяет прошлые сессии по трём осям: выполнены ли обещания и требования;
  логичны ли решения и их качество; насколько глубоко изучен вопрос — что агент
  упустил. На реальном прогоне так за неделю разобрали **271 диалог** и нашли
  агентов, которые задачу сделали, **но при планировании ввели в заблуждение** —
  в живом чате это проходит мимо и уводит в неверные решения.
- **Продолжить, когда кончился контекст — без потери деталей.** `/compact`
  затирает подробности. Вместо этого открой новую сессию: она прочитает **логи**
  предыдущей и продолжит с её выводов, не сжигая контекст заново на то, что уже
  изучено. Исходная сессия остаётся целой — для аудита и поиска. Новая сессия
  может быть в **любом** агенте: история сводится независимо от инструмента.
- **Питает твою систему памяти.** Ведёшь память и саммари по методу Карпатого
  или своему? `ai-r` даёт для AI-чатов то же, что ты делаешь с перепиской, —
  разобранные сущности, из которых строишь постоянную память важных деталей.
- **Вспомнить, что и зачем делали.** Зачем правили этот файл? Почему завели это
  правило? Находишь сессию, где файл менялся, и читаешь запрос *перед* правкой.

## Чем отличается от инструментов поиска сессий

Появилась горстка кросс-агентных инструментов, читающих историю нескольких
агентов (`jazzyalex/agent-sessions`,
`Dicklesworthstone/coding_agent_session_search`, `hacktivist123/agent-session-resume`).
Почти все они — про **поиск и таймлайн**: найти *сессию*, пролистать историю.

`ai-r` идёт глубже: он извлекает **план, намерение и авторство как готовые
сущности**, на которых ты строишь память. Поиск находит текст — `ai-r`
отвечает, **почему**. Технически поисковый инструмент тоже мог бы выудить план
из текста сессии, но он не отдаёт его наружу в разобранном, едином виде — у
`ai-r` это главная поверхность.

| Возможность | Вьюеры одного агента | Кросс-агентные search-тулы | `ai-r` |
|---|---|---|---|
| Читает логи >1 агента | Нет | Да | Да — Claude, Codex, OpenCode, Antigravity, Pi |
| Программная поверхность | В основном GUI/TUI | В основном TUI/CLI/app | **MCP + CLI + Python SDK** |
| Атрибуция (правка/команда → агент + intent) | — | Частично | Да — `find-file-edits` / `find-tool-calls` |
| Аудит-реплей (реконструкция изменений сессии, без git) | — | Редко | Да — `session_diff` |
| Извлечение плана (final vs draft, нормализовано) | — | — | Да — `plan` |
| Скоуп | Вьюер | Поиск / резюм / память | **Read-only ядро извлечения** |

*Столбцы конкурентов — по их публичным докам на 2026-07; где возможности
неясны, мы скорее занижаем, чем переоцениваем.*

Мы сознательно **не** соревнуемся по охвату агентов, скорости или богатству
TUI. Клин `ai-r` — извлечение «почему» и структурные сущности для
машинного потребления.

## Проверено в деле

`ai-r` уже читает собственную историю разработки — по всем пяти агентам. На нём
держатся реальные инструменты (живут отдельно, поверх его read-only API):

- **аудитор** — свежий агент холодно проверяет, что предыдущий реально сделал и
  решил. Так ловили агентов, которые тихо привирали про план.
- **суммаризатор** (`export rounds`) — рендерит сессию в готовый
  документ-передачу (handoff).
- **ai-local-reader** — read-only скилл: аудит прошлых сессий с диска по всем
  агентам.

Эти инструменты — на стороне рабочего процесса, вне этого репозитория. Сам
`ai-r` только читает и отдаёт данные.

## Поддерживаемые агенты

| Агент | Хранилище | Парсер |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite (авто-детект snap/flatpak) |
| Antigravity | `~/.gemini/antigravity/brain/` | JSON / markdown brain-директории |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

Не ваш агент? Добавить шестого — это **один модуль-парсер**; read-only паттерн
портируется на любой инструмент за минуты. См.
[CONTRIBUTING.md](./CONTRIBUTING.md).

## Поверхности

`ai-r` даёт одну и ту же силу чтения тремя способами:

- **MCP-сервер** (`ai-r-mcp`) — 13 инструментов через stdio JSON-RPC, так что
  любой MCP-агент дёргает его напрямую (рекомендуется). Регистрация — см.
  [docs/mcp-registration.md](./docs/mcp-registration.md).
- **CLI** (`ai-r`) — субкоманды для скриптов и ручного использования
  (`list` / `read` / `search` / `find-file-edits` / `find-tool-calls` /
  `file-frequency` / `detect-agent` / `export rounds`). Операторы поиска —
  [docs/search-operators.md](./docs/search-operators.md).
- **Python SDK** (`from ai_r.parsers import ...`) — парсеры, типизированные
  модели `Session`/сообщений и событийные вербы, чтобы строить свои
  инструменты.

### Словарь методов (SSOT)

Блок ниже сфреймлен из [`docs/methods.ru.md`](./docs/methods.ru.md) —
русского зеркала первоисточника ([`docs/methods.md`](./docs/methods.md),
англ.) по публичным вербам и пресетам. Держится синхронным с
маркер-блоком того файла.

<!-- methods:start -->

## Verbs

| verb | purpose | parameters |
|---|---|---|
| `query` | filter/search session events — emitted `text` is a ~160-char **preview** (cut applied after redaction; a real cut carries a trailing `…` + `text_truncated: true`), full body on-demand via `get_body`; `with_intent=True` → a top-level `intent` on each event (the same `previous_user_intent` as legacy); a `tool_call` event carries an `is_error` outcome ref when its result is correlatable plus wrapper-aware `tool_kind`/`tool_resolved` (see *Tool calls under wrappers* and *Output bounds & outcome* below) | type, agent, session (uuid \| **list of uuids** — the union of those sessions' events; empty list / non-string item fails loud), since, until, file, tool (also matches resolved names), tool_kind(edit\|write\|read\|bash\|task\|skill\|mcp\|web\|other), text, sort(relevance\|date\|semantic), relative_to+direction(prev\|next)+n(1\|all), step_type, limit, with_intent, noise(include\|exclude\|only), project_dir, redact; kind/parent/group — stubs (Phase 3) |
| `plan` | normalized plan atoms of a session (final vs drafts, grouped by task); by default the **final** plan's full text is inlined (`body` + `body_source`) and every «plan quote → user comment» pair is returned under `feedback` — with `version`/`plan_version`, `section` anchors and `round` numbers (see *Plan iterations* below) | session, kind(draft\|final\|completed_major), group=task, agent, redact, bodies(final\|none), feedback(true\|false), rounds(all\|last) |
| `get_body` | on-demand body by event/plan id; a plan-feedback ref `"<session>:pf<N>"` resolves to the FULL raw plan response (type `plan_feedback`); returned body/text is bounded by `max_chars` (default 500k) → over-long bodies are cut with a marker and flagged `body_truncated` | id, shallow, max_chars, redact |
| `aggregate` | rollup over rows (query/find_file_edits/session-inventory) → `{groups, totals}`; `rank_by=stats` gives the session_stats order (sessions→edits→label), `kind_split=True` adds `kind_split_available`/`note`; the `tokens` metric folds per-row token blocks into `{input, output, reasoning, cache_read, cache_write, total, exact, estimated, unknown}`, the `component_tokens` metric folds per-row per-component blocks (`user_turn`/`assistant_turn`/`thinking`/`plan`/`tool_call.<kind>` + `estimated`/`unknown`) (see *Token usage* below) | rows, group_by(field\|callable), metrics ⊆ count\|sessions\|edits\|intents\|agents\|messages\|files\|tokens\|component_tokens, rank_by(default\|stats), kind_split |
| `diff` | stitch edit-rows into a per-file unified diff (bodies on-demand via message_index; `intent` taken from the row when `query(with_intent)`) → `{files:[{file,edits,diff,hunks}], count, caveats}` | rows, per_file=True, format=unified, redact |
| `detect_current` | runtime identity (env/fs, outside session-query) → `{session_id, agent, candidates[], verified, self}` | agent (hint) |

## Presets

| preset | expansion |
|---|---|
| `intent(event, n)` | `query(relative_to=event, direction=prev, n)` |
| `reaction(event, n)` | `query(relative_to=event, direction=next, n)` |
| `plan(session, kind, group=task)` | `query(type=plan_event, …)` → normalized + kind-tagged (final/draft/completed_major) |
| `session_stats(group_by, with_tokens)` | builds per-session inventory rows → `aggregate(rows, group_by, rank_by=stats, kind_split=True)` → projection to the legacy totals shape; `with_tokens=True` adds a folded `tokens` block per group + totals (see *Token usage* below) |
| `session_diff(uuid, agent≠codex)` | `diff(query(type=edit\|write, session=uuid, with_intent=True) with file-ref)` → projection (no file-level `hunks`) |
| `incidents(agent, session, since/until, category, confirmed, reaction_window, limit, noise, project_dir)` | ONE `query(type=tool_call, tool_kind=bash)` scan → deterministic danger dictionary over the extracted command → bilingual (ru+en) regret scan over the next `reaction_window` messages → two-step `confirmed` verdict (see *Audit presets* below) |
| `network(agent, session, since/until, kind, risk, domain, limit, noise, project_dir)` | ONE `query(type=tool_call, tool_kind=web)` scan → request target (`url`/`query`) extracted from the call's own input → deterministic risk dictionary (see *Audit presets* below) |

## Audit presets: `incidents` & `network`

Both follow the preset rule — one call = a baked chain of the base verbs with a deterministic algorithm inside, never a second engine. Zero LLM, zero guessing: every verdict is a dictionary/regex hit on transcript evidence or an honest `null`.

- **`incidents` (F4.1)** — "where did an agent run something destructive — and did it then apologise?". ONE `query(type="tool_call", tool_kind="bash")` scan supplies the candidates; a deterministic **danger dictionary** (19 patterns across `fs`/`git`/`db`/`net`, harvested from public agent-guardrail rule sets and calibrated on real host history — a Bash `description` alone never fires, `--force-with-lease` is not force-push) selects dangerous commands; a bilingual (ru+en) **regret dictionary** scans the next `reaction_window` messages (default 6) for an apology/rollback reaction — the two-step check behind `confirmed`, never inferred. Each record carries the query event `id` (context on-demand via `relative_to`/`read_session`), `patterns` + `categories`, a char-capped `command` fragment centred on the hit, tri-state `is_error` (`null` where the agent's format has no correlated outcome signal) and `reaction` (marker labels + capped preview; `null` when unconfirmed). `count`/`confirmed_count`/`by_pattern` always reflect the FULL match set independent of `limit`; unknown `category`/`confirmed` values fail loud; zero incidents → empty-result `diagnostics`. Documented caveat: a dictionary cannot tell mention from execution — an `echo`-ed dangerous string can still match. SSOT `ai_r.incidents`.
- **`network` (F4.3)** — "where did an agent reach out to the network — and how risky did those requests look?". ONE `query(type="tool_call", tool_kind="web")` scan supplies the candidates (Claude `WebFetch`/`WebSearch`, OpenCode `webfetch`, Codex `web_search` — surfaced from `web_search_call` rollout records, Gemini/Antigravity `web_fetch`/`google_web_search`; Pi records no web tool — honest absence); the request target (`url`/`query`) is extracted from each call's own input (nothing extractable → honest `null` fields and `kind: null`, never guessed from the tool name); a deterministic **risk dictionary** assesses each request — `plain_http`, `credentials_in_url`, `secret_in_url`/`secret_in_query` (the F2.1 redaction patterns double as the detector — one vocabulary, two uses), `ip_literal_host`, `private_or_local_host`, `punycode_host`. Each record carries the query event `id`, derived `kind` (`fetch`/`search`), char-capped `url`/`query` (the cap is applied AFTER redacting the full string, so a boundary-sliced secret never leaks partially), `domain`, `risks` and tri-state `is_error`. `count`/`risky_count`/`by_domain`/`by_risk` always reflect the FULL match set independent of `limit`; unknown `kind`/`risk` values fail loud; `domain` matches equals-or-subdomain; zero requests → empty-result `diagnostics`. **Boundary: MCP-mediated network access (browser MCP tools) stays under `tool_kind="mcp"` and is never guessed into the audit — transcript-recorded name+input is the only signal ai-r has.** SSOT `ai_r.network`.

## Legacy tools: presets over verbs (Phase 3b)

Phase 3b enriched the verbs so old tools became thin presets **with byte-identical output, proven on REAL data** (frozen snapshot `~/.claude`, so the live vault doesn't mutate mid-run — that produced false mismatches). The legacy suites (`test_session_stats`/`test_session_diff`) are green — the second half of the compatibility proof.

**Ported to verbs (byte-parity proven):**

| tool | preset over verb | proof |
|---|---|---|
| `session_stats` | `aggregate(rank_by=stats, kind_split=True)` over per-session inventory rows | 8/8 (group_by∈agent\|dir\|date\|kind × top∈8\|0) EQUAL on the snapshot; the key is `rank_by=stats` reproducing the sessions-first rank, `kind_split` giving `kind_split_available`/`note` |
| `session_diff` (≠codex) | `diff(query(edit\|write, with_intent=True))` | 12/12 real Claude sessions EQUAL; the key is `with_intent` returning `intent`, a single chronological stream giving the same file order, the edit\|write filter excluding `Read` (else extra files) |

**Codex — exception in `session_diff`:** codex writes files via shell-exec, and the target is recovered by scanning the command line, which the event stream does NOT do → shell-redirect edits would vanish from the `query` fold. So the codex branch of `session_diff` keeps the legacy `_scan_session` (byte-parity for all agents).

**Stay separate (justified):**

| tool | why NOT a preset |
|---|---|
| `find_file_edits` / `find_tool_calls` | the record carries `session_title`/`session_date`/`assistant`/`input`, which are NOT in a `query` event; `find_tool_calls` additionally carries per-record `is_error` (correlated tool-call outcome) and `output` (correlated tool-result content, char-capped); reproducing them = re-reading the session (not a *thin* preset but a second parse over events — strictly slower) + loss of codex shell-redirect edits. `intent` is now reproducible (`with_intent`), but the other fields are not. SSOT of the rich edit/tool record |
| `search_sessions` | session-granular + BM25 session snippets; `query` is event-granular (turn/tool) → no clean 1:1 |
| `detect-agent`/`detect-session` (CLI) | the CLI prints the agent `source` and 6 output modes (list/first/strict/self/fingerprint/`--json`/`--count`) + a WARN line; the `detect_current` dict does not provide this |

## Plan atom (normalized, agent differences hidden)

`Plan { id, session_id, agent, title, task_id, kind: draft\|final\|completed_major, path?, steps?, status?, refs[], sha256 }`. Body/steps — on-demand via `get_body(id, shallow?)`. `shallow=True` → only the task's final, draft bodies dropped (scenario S6).

**Grouping by task = `task_id` (stable key):** for Claude it's the plan slug `plans/<slug>.md` (Write carries the path directly; `ExitPlanMode` without a path inherits the slug of the nearest preceding plan-Write in the session; if there is no slug yet — fallback to the normalized title). For Antigravity — the `implementation_plan.md` path. For Codex (no file) — the normalized title (a continuous `update_plan` run). Keyed by slug, NOT by title, because the title drifts within one iteration chain (decorations change the heading) — on real data that split one task into several. In a group the last plan_event by (ts, seq) = `final`, the earlier ones = `draft`; strictly earlier tasks (a DIFFERENT slug) = `completed_major`. The internal parser→signal table (`ExitPlanMode`/`Write plans/*.md`/`update_plan`/`implementation_plan.md`) is an implementation detail, invisible from outside.

## Plan iterations (final body, feedback, versions, rounds)

By default `plan` returns everything a consumer needs to replay a plan-approval iteration without inlining every draft (measured ≈×3.7 cheaper than "all bodies"; F3.4):

- **Final body inline** — the `final` plan carries `body` + `body_source`. The AUTHORITATIVE text is the user-edited plan carried by the approval response (`"approval_edited_by_user"` — the plan file on disk can diverge from what was actually approved), falling back to the plan signal (`"plan_signal"`); honest `null` for steps-only plans (Codex). Drafts stay references (bodies via `get_body`); `bodies="none"` restores reference-only atoms.
- **`feedback` pairs** — ALL «plan quote → user comment» pairs extracted from the user's plan responses, chronological. Each pair carries `plan_id` (the exact revision it answered, correlated by call id — a **rejected plan-file `Write`** correlates exactly like an `ExitPlanMode` verdict), `verdict` (`rejected` | `stay_in_plan_mode`), `quote` (`null` for a free-text comment), verbatim `comment`, `ts` and a `ref` (`"<session>:pf<N>"`) that `get_body` resolves to the FULL raw response (type `plan_feedback`). Technical failures and bare no-comment rejections are filtered out; `feedback=false` omits the list. Only agents with an interactive plan-approval flow have the signal (today: Claude `ExitPlanMode`); others honestly contribute nothing — never fabricated.
- **Versions, sections, rounds (v2)** — every plan atom carries `version`, its 1-based revision number within the task group in chronological `(ts, seq)` order (drafts are `v1…vN-1`, the final is `vN`; numbering restarts per task). Every feedback pair carries `plan_version` (the answered revision's number, `null` without call-id correlation) and `section` — the heading of the plan section the quote anchors to: the user selects quotes from the RENDERED plan, so both the quote and each section of the raw markdown source are compared through the same markup-stripping normalization; a quote that matches NO section — or MORE than one — gets an honest `null` anchor, never a nearest guess. Pairs are grouped by `round` — the 1-based feedback-round number within the session — and `rounds="last"` keeps only each session's final round (`"all"` default; anything else fails loud). Plan atoms themselves are unaffected by `rounds`.

Redaction (F2.1) covers plan bodies, quotes, comments and raw responses. Core SSOT: `ai_r.events.plan_feedback`.

## Tool calls under wrappers (`tool_kind` / `tool_resolved`)

Every tool call is classified wrapper-aware (F3.1). Each `tool_call` event (`query`) and every `find_tool_calls` record carries `tool_kind` — one of `edit|write|read|bash|task|skill|mcp|web|other` — and, when a wrapper's input names the real actor, `tool_resolved`: the subagent type under a spawn wrapper (Claude `Task`/`Agent` → `input.subagent_type`, OpenCode `task` → `subagent_type`, Codex `spawn_agent` → `agent_type`), the skill name under Claude `Skill` (`input.skill`) / OpenCode `skill` (`input.name`) / `SlashCommand` (`input.command`, reduced to the bare command token), or `"<server>:<tool>"` for a Claude-style `mcp__<server>__<tool>` name. Honest per-agent signals only: a wrapper whose input carries no name key gets no `tool_resolved` (never guessed); Codex/OpenCode/Pi record MCP calls under bare or underscore-joined names with no reliable server delimiter, so no `mcp` detection there. `query` takes a `tool_kind` facet (exact match, unknown value fails loud) and the `tool` facet also matches resolved names (`tool="commit"` finds the SlashCommand that ran the `commit` skill); `tool_kind`/`tool_resolved` are hoisted to top-level event fields, so `aggregate(group_by="tool_kind")` works on query rows directly. Backward-compat: the event `type` keeps the base `tool_call(<sub>)` subtype — no counts or existing filters change; `tool_resolved` passes the F2.1 emission-time redaction. The `web` kind (plus the Codex `web_search_call` rollout signal surfaced by the parser) feeds the `network` preset. SSOT `ai_r.events._common.resolve_tool` + `TOOL_KIND`.

## Session outcome (`read_session.outcome`)

`read_session` carries an `outcome` block (F2.3) — `{status, signals, user_verdict, markers, tool_results, tool_errors, error_rate, error_rate_reliable}` with `status ∈ success|failure|mixed|unknown`. Two honest signals, never a guess: (1) **tool-call error rate** — the share of tool results the agent itself flagged as failed; a real source flag exists only for Claude (`tool_result.is_error`) and OpenCode (`state.status == "error"`), so for Codex/Pi/Antigravity `tool_errors`/`error_rate` are `null` (`error_rate_reliable: false`) — mirrors `find_tool_calls.is_error_reliable`; (2) **user-verdict dictionary** — bilingual (ru+en) success/failure markers matched against the last 3 *human* user turns only (assistant self-reports are never trusted; XML wrappers / `[...]` placeholders / `Caveat:` preambles skipped). Decision table: negative verdict → `failure`; positive → `success` (`mixed` when errors dominate); neutral + dominant errors → `failure`; otherwise `unknown` (empty `signals` ⇔ `unknown`). Thresholds and the dictionary are calibrated on real history ("dominant" = `rate ≥ 0.5` across `≥ 4` results) — conservative by design. The block contains only ai-r-authored strings and dictionary labels (never raw session text), so it needs no redaction pass. SSOT `src/ai_r/outcome.py`.

## Token usage (`with_tokens` + the `tokens` / `component_tokens` metrics)

`session_stats(with_tokens=True)` reads every matched session's token usage from the agent's own files **at request time** (nothing background, no index) and adds a folded `tokens` block to each group and to `totals`; `aggregate` accepts a `tokens` metric that folds per-row token blocks the same way (F3.3). The block is `{input, output, reasoning, cache_read, cache_write, total, exact, estimated, unknown}` — sums are `null` when no row carried the field (never a fabricated 0) and the provenance counters always satisfy `exact + estimated + unknown == rows`. Per session the numbers are **exact** where the format records usage (Claude per-call `message.usage`, streamed duplicates deduplicated by `(message.id, requestId)`; Codex last cumulative `token_count`; OpenCode `message.data.tokens`; Pi per-assistant-message `usage`) — via a per-parser `read_token_usage` (feature-for-all-where-signal); a session without a recorded signal (Antigravity, or older data) gets a transcript-volume **estimate**, labeled `source="estimate"`: tokenized by the **optional** `tiktoken` dependency (`pip install "ai-r[tokens]"` / `AI_R_EXTRAS=tokens bash install.sh`) when installed, else a rough chars/4 heuristic — degradation, never a crash; no signal at all stays honest `unknown`. Only ai-r-computed integers and labels are emitted (no raw session text), so the block is outside the redaction surface by construction. Default `with_tokens=false` keeps byte-identical historical output. SSOT `ai_r.tokens`.

`read_session(with_tokens=True)` adds a **per-component** view (F3.3 follow-up), still integers-and-labels only → outside the redaction pass by construction. It attaches TWO keys: `summary["tokens"]` — the same flat exact-or-estimate `session_tokens` block above; and `summary["component_tokens"]` — the estimated transcript volume split across ai-r's **existing event taxonomy**: `{user_turn, assistant_turn, thinking, plan, tool_call: {<tool_kind>: n}, total, source:"estimate", estimator}`. This reuses the same classifiers the event layer uses (`resolve_tool` for `tool_kind`, the plan-signal detector, the user/assistant role) — a measurement over the established components, not a second classifier. `user_turn` is the question/request text, `assistant_turn` the answer, `thinking` the reasoning; **plan-authoring** tool calls (`ExitPlanMode` / `Write plans/*.md` / Codex `update_plan`) count under `plan`, NOT `tool_call` (no double count); every other call's `input` plus its `tool_result` `content` (correlated by `tool_use_id`; an orphan result → `other`) is bucketed by `tool_kind`. All surfaces share ONE estimator (`tiktoken` when installed, else `chars/4`), so the block is uniformly `source="estimate"` and is never mixed into the exact `tokens` tier; `total` sums every component; an all-empty transcript yields `null`. `aggregate` accepts a matching `component_tokens` metric that folds per-row blocks (per-component sums + `estimated`/`unknown` provenance; a component no row carried stays absent, never a fabricated 0). On top of the session block, projected message entries carry a **per-message exact** `tokens` block wherever the format records per-message usage — Claude (per API call, deduplicated by `(message.id, requestId)`, the dedup decided on absolute positions BEFORE pagination), OpenCode (`message.data.tokens`), Pi (`usage`); Codex (cumulative-only), Antigravity (no usage) and user turns carry NO `tokens` key at all (absent, never a null). `read_session(include_subagents=True)` additionally attaches `summary["subagent_rollup"]` — the parent's `component_tokens` plus one per spawned subagent child (`children_of(parent_uuid)`) and a folded `total`; a childless parent (or Antigravity, which records no `parent_uuid`) yields an empty `children` list. The CLI mirrors it: `ai-r read <uuid> --with-tokens` prints a human `COMPONENT | TOKENS | SOURCE` table (`--json` emits the block); MCP stays JSON. Default `with_tokens=false` → output byte-identical to before. SSOT `ai_r.tokens` (`component_tokens`) + `read_session` in `ai_r.mcp_server`.

## Semantic sort (`sort="semantic"`)

The text-search surface (`query` with a `text` facet, `search_sessions`) accepts `sort="semantic"` (F5.1, optional `ai-r[semantic]`): the BM25 top-50 candidates are re-ranked by *meaning* with a **local** multilingual embedding model (`intfloat/multilingual-e5-small`, int8 ONNX, run directly through onnxruntime + tokenizers — no torch; cross-lingual ru↔en, synonyms). No persistent index: texts are embedded at request time, nothing stored. The top-50 pool is a cost budget, not a quality cut-off — there is deliberately NO similarity threshold, so results are only re-ordered, never dropped; within the pool the blended score is **75 % meaning + 25 % word match** (both min–max normalized) — meaning dominates, the word share protects exact-term hits and breaks ties; results beyond the pool keep their BM25 order. The response carries a `semantic` report: `active: true` (+ model, candidate count, blend weight) or the honest degradation `active: false` + plain-words `reason` + `fallback: "bm25"` — without the optional deps (`pip install "ai-r[semantic]"`) or the model files (`AI_R_EXTRAS=semantic bash install.sh` downloads them; override via `AI_R_SEMANTIC_MODEL_DIR`) the order falls back to plain BM25, never a crash, and the default sorts (`relevance`/`date`) never touch the module at all. Embedding sees RAW text while emission stays redacted; reference-by-default unchanged. SSOT `ai_r.semantic`.

## Noise filter (session-level)

A session is *noise* when it is not a top-level human-driven conversation — today that means **spawned subagent (sidechain) sessions**: `kind == "subagent"` or `parent_uuid` set (criterion SSOT: `src/ai_r/parsers/_noise.py`). `query`, `list_sessions` and `search_sessions` take `noise ∈ {include, exclude, only}` (default `include` — fully backward-compatible): `exclude` keeps only top-level agent sessions, `only` keeps only the subagent tree (audit view). The filter applies at the *session* level before any message is read (an excluded session costs nothing), composes with the other filters by AND (incl. `list_sessions(kind=…)`), and an unknown mode fails loud (`invalid_argument`). In `query` it is ignored on the `relative_to` walk (the anchor pins one concrete session), like every other facet.

**Subagent-detection coverage (parser-internal normalization, one public criterion):** **Claude** — `subagents/` directory layout + sidechain `parentUuid`; **OpenCode** — `session.parent_id`; **Codex** — `session_meta.payload.thread_source == "subagent"` + `parent_thread_id` (incl. the nested `source.subagent.thread_spawn.parent_thread_id` fallback); **Pi** — the `parentSession` header field. **Antigravity** — no parent signal in the format → always `kind="agent"`, never noise. Warmup/scaffold sessions are **not** classified as noise: no agent format carries a reliable cheap marker for them and a title heuristic would misfire, so the criterion stays exact (noise == subagent) rather than guessed.

## Claude session sources (CLI + Desktop overlay)

The Claude parser scans **two roots** and merges them into one session list (F1.3):

- **CLI root** — `~/.claude/projects/<slug>/<uuid>.jsonl`: the transcripts (`$AI_R_HOME/.claude/projects` when `AI_R_HOME` is set).
- **Desktop root** — `~/.config/Claude/claude-code-sessions/<device>/<workspace>/local_*.json` (`$AI_R_HOME/.config/Claude/claude-code-sessions` under `AI_R_HOME`): the Claude **Desktop** app's own store. It holds **metadata only** — one JSON object per session (`sessionId`, `cliSessionId`, `title` + `titleSource`, `cwd`, epoch-ms timestamps, `model`, `permissionMode`), NOT transcripts: a Desktop-launched session's transcript still lives in the CLI root under `cliSessionId`.

**Merge rules:** dedup key is the session uuid (`cliSessionId` == the CLI JSONL stem) — a session visible in both roots is returned ONCE, enriched: the Desktop `title` wins (it is the title the user sees in the app, hence what they will search for; the CLI-derived title is preserved as `extra["cli_title"]`). Origin is marked in `extra["source_root"]`: `"desktop"` = the session was driven from the Desktop app (a *launch-surface* signal; F1.4 surfaces it first-class as `launch_surface="claude-desktop"`), `"cli"` = plain CLI session (`launch_surface="claude-cli"`). A uuid present ONLY in the Desktop store (transcript deleted) still appears as a **reference-only** session — `message_count == 0`, reading its messages returns an empty list (honest answer, not an error), `path` points at the metadata JSON. A missing root is skipped, never an error. The overlay applies uniformly to `list_sessions` / `read_session` / `search` / `session_exists`; `source_roots()` reports both roots so empty-result diagnostics can name them. Hermetic-test note: an explicit `base_dir` **without** an explicit desktop root pins the scan to the CLI root only, so fixture-scoped callers never leak the real HOME.

## Session origin (`project_dir` + `launch_surface`)

Every session summary (`list_sessions` / `read_session` / `search_sessions` candidates) carries two first-class origin fields next to `kind`/`parent_uuid`, both `null` when the source format has no signal — **absence is honest, never fabricated**:

- **`project_dir`** — the project directory the session ran in. Per-agent signal (parser-internal normalization, one public field): **Claude** — the record-level `cwd` of the CLI transcript; fallback to the Desktop metadata `cwd`/`originCwd` (F1.3 overlay), then to a **filesystem-verified** decode of the `projects/<slug>` storage encoding (the slug flattens `/` and `.` to `-`, so a dash inside a real name is ambiguous — the decoder searches the possible segment boundaries and accepts only a path that actually exists as a directory; unverifiable → `null`, no guessing). **Codex** — `session_meta.payload.cwd`. **OpenCode** — the `session.directory` column (legacy DBs predating the column degrade to `null` via a legacy-SELECT fallback, enumeration never breaks). **Pi** — the session-header `cwd`. **Antigravity** — the format carries no structured cwd/directory field → always `null`.
- **`launch_surface`** — the concrete surface the session was driven from, only where the data makes it distinguishable: **Claude** — `"claude-cli"` | `"claude-desktop"` (from the F1.3 Desktop-overlay signal). **Codex** — the raw `session_meta.payload.originator` string passed through verbatim (observed: `"codex_vscode"`, `"Codex Desktop"`; no invented taxonomy on top of the raw value). **Antigravity** — `"antigravity-ide"` | `"antigravity-cli"` (which brain root holds the session: `~/.gemini/antigravity/brain` is the IDE app, `~/.gemini/antigravity-cli/brain` is the CLI). **OpenCode**/**Pi** — no signal in the format → always `null` (OpenCode's `agent` column is the *mode* — plan/build — not a surface).

**`project_dir` filter** on `list_sessions` and `query`: keeps only sessions whose `project_dir` equals the given path **or is a descendant of it** — path-boundary aware (`/a/b` matches `/a/b` and `/a/b/sub`, never the sibling `/a/bc`), trailing slashes ignored, no other normalisation (`~`/`..`/symlinks are compared as recorded). Chosen over exact-only because "sessions of this project" must include sessions started in a subdirectory of the project root. Sessions with `project_dir=null` never match (absence is not a wildcard). Applied at the *session* level before any message is read (like `noise`), composes with the other filters by AND, ignored on the `relative_to` walk, and an empty/blank filter value fails loud (`invalid_argument`). Semantics SSOT: `src/ai_r/parsers/_common.py::project_dir_matches`.

## Resume command (`resume_command`)

Every session summary carries `resume_command` (F2.2, next to `project_dir`/`launch_surface`): the exact shell one-liner that reopens the conversation in its agent's CLI, or `null` when no real command exists — **absence is honest, never fabricated; the field is text only, ai-r never executes it**. Commands are verified against the installed CLIs' own `--help`, not invented:

| agent | command | why this shape |
|---|---|---|
| Claude | `cd <project_dir> && claude --resume <uuid>` | `claude --resume` resolves the id against the project store of the *current working directory* → the `cd` prefix makes the command work from any shell; with `project_dir=null` the bare `claude --resume <uuid>` is emitted (works only when already inside the original project dir). A **reference-only Desktop session** (transcript deleted, F1.3) → `null` — nothing to resume. |
| Codex | `cd <project_dir> && codex resume <uuid>` | `codex resume <SESSION_ID>` is id-addressable in the global store (the cwd filter only affects the interactive picker); the `cd` prefix keeps the continued session in its original directory (Codex always records `cwd` → the prefix is always present). |
| OpenCode | `cd <project_dir> && opencode --session <id>` | main-command flag `-s, --session` ("session id to continue"); project-scoped TUI → `cd` prefix when `session.directory` is known (legacy DBs without the column → bare command). |
| Pi | `cd <project_dir> && pi --session <path>` | `pi --session <path\|id>`: the *id* lookup is scoped to the current project's session dir, while the recorded session-**file path** is unambiguous from anywhere → the path form is emitted. |
| Antigravity | `null` | sessions are IDE brain directories with no CLI resume verb; the local `gemini` CLI's `--resume` addresses its **own** store by index/`latest`, not brain-dir ids → no real command exists. |

Cross-agent rules: **subagent (sidechain) sessions are never resumable** (`kind="subagent"`/`parent_uuid` set → `null` — the CLIs resume top-level interactive conversations, not spawned tool threads); every interpolated value (uuid/path/dir) is shell-quoted. Semantics SSOT: `src/ai_r/resume.py::resume_command`.

## Output bounds & tool-call outcome

**Bounded output (untrusted sessions can be huge — the surface never returns unbounded bytes):** `find_tool_calls` caps each record's `input`/`assistant`/`intent`/`output` fields (over-long values cut with a `…[truncated]` marker and named in a per-record `truncated_fields`) and stops appending once a total-response byte budget is hit, flagging `output_truncated`; this is distinct from the count-based `truncated` (more records exist). `get_body` bounds the body via `max_chars` (`body_truncated`). Tool input larger than 1 MB is never JSON-decoded (returned verbatim) — a shared guard on the event stream and `find_tool_calls` alike. `read_session` renders a tool result as `[tool_result ok: <snippet>]` or `[tool_result ERROR: <snippet>]` (was a bare `[tool_result]`).

**Adaptive output truncation (`output_mode`):** the per-record `output` cap is `_OUTPUT_CHARS_CAP = 2000` chars. How that budget is spent is controlled by `output_mode ∈ {"head", "tail", "smart"}`. The default (`output_mode=None`) is **adaptive per record**: a record with `is_error == True` is truncated `"smart"` (surface the error lines — `error`/`fatal`/`traceback`/… — plus the tail, so an error at the *end* of a long log is not lost to a head-only cut), while a successful record is truncated `"head"` (legacy behaviour). An explicit `output_mode` forces one strategy for every record. `smart`/`tail` may return up to ~2× the cap to keep both the surfaced lines and the tail; whenever `output` is cut it is still named in that record's `truncated_fields`.

**Filtering `find_tool_calls` (all optional, composed by AND):** beyond `tool_name`/`tool_name_pattern`, records can be narrowed by `input_contains` (case-insensitive substring over the serialized tool input / command text), `output_contains` (ci substring over the correlated `output`), `output_excludes` (drop a record whose `output` contains the marker — a caller-supplied noise filter, e.g. a harness security-gate line, `"user rejected"`, `"MANUAL COMMIT BLOCKED"`; **no such list is hard-coded in the core**), and `is_error` (tri-state: `None` = all, `True` = errors only, `False` = successes only). All filters intersect (AND). There is **no** dedicated "error + domain" verb: that pairing is a *composition* — e.g. `find_tool_calls(input_contains="git", is_error=True)` returns the real command failures of a chosen domain (`git` is just an example domain, not a special case).

**`is_error` (tool-call outcome) is cross-agent best-effort:** **Claude** and **OpenCode** carry a real success/error flag (Claude's `tool_result.is_error`; OpenCode's `state.status == "error"`). **Codex** and **Pi** expose no error field on their result records → `is_error` is always `False` (absence of a flag, not a proof of success). **Antigravity** emits no tool-result records at all → no outcome signal. Consumers must not read a cross-agent `is_error=False` as "verified success" for Codex/Pi/Antigravity. `find_tool_calls` now carries the same `is_error` per record, plus the correlated `output` (tool-result content, char-capped) — correlation is by tool_use_id (Claude `tool_use.id` / OpenCode `callID`); with the same best-effort caveat (`is_error` is authoritative only for Claude/OpenCode, and defaults to `False` for Codex/Pi/Antigravity or when no result correlates). To make that honesty machine-readable, each `find_tool_calls` record also carries `is_error_reliable` (bool): `True` for Claude/OpenCode (a real flag backs the value), `False` for Codex/Pi/Antigravity (no source → `is_error` is always `False` and may **undercount** true failures). A consumer filtering `is_error=True` should read `is_error_reliable` to know whether a `False` means "verified success" or merely "no signal".

## Redaction (secrets masked on output)

Real transcripts routinely contain pasted secrets, so **every method that emits session-derived text masks them on output by default** (F2.1). Emitting surfaces: `query` (`text`/`intent`), `get_body` (`text`/`body`/`title`/`steps`), `plan` (title/steps/refs), `diff` & `session_diff` (diff text/hunks/intents), `read_session` (title + message content), `search_sessions` (title/snippet), `list_sessions` (title), `find_file_edits` & `find_tool_calls` (title/intent/assistant/input/output). Each replacement is `[REDACTED_<TYPE>]`; when anything was masked the response carries a `redactions` type→count dict (absent = nothing masked). `redact=false` on any of these returns the raw content. `session_stats`/`aggregate` emit only counts/labels derived from rows the caller already holds — no session text of their own, hence no `redact` parameter; `detect_current` reads the runtime env, not transcripts.

**Types:** `PRIVATE_KEY` (PEM blocks), `AWS_KEY`/`AWS_SECRET`, `GITHUB_TOKEN`, `GITLAB_TOKEN`, `ANTHROPIC_KEY`, `OPENAI_KEY`, `SLACK_TOKEN`, `URL_CREDENTIALS` (`user:pass@` in URLs — only the credential span is masked), `BEARER_TOKEN` (value only, the `Bearer` prefix survives), `GENERIC_SECRET` (an explicit secret-ish key name — `password`/`token`/`api_key`/… — assigned a token-shaped value). Pattern SSOT: `src/ai_r/redact.py`. **Bias against false positives:** uuids, git hashes and prose like `sk-learn` or `Bearer authentication` never trip (value patterns require a digit; the generic catch-all requires a key name + `:`/`=`); the honest trade-off is that an all-letter password under a generic key is not masked.

**Emission-time only:** redaction never touches scanning or matching — every filter (`text`, `input_contains`, search queries, …) matches the RAW stored text, so searching for a literal secret still finds its session (only the displayed output is masked), and a `[REDACTED_*]` placeholder can never match as a search term. The searched body/haystack (`search_sessions`, `query text`) now folds each message's captured `Message.thinking` (model reasoning) alongside its text, so reasoning is searchable for every agent that marks it — Claude/Codex/OpenCode/Pi (feature-for-all-where-signal; Antigravity has no reasoning signal). This reasoning was previously dropped by Claude/Codex/Pi and lived inside `Message.text` for OpenCode; it now has its own field. The empty-result diagnostics say this out loud: a filter value that IS a placeholder, or that itself looks like a secret, earns a hint explaining that redaction is output-only and suggesting `redact=false` for raw output.

## Empty results & session lookup

**Empty-result diagnostics (a zero-result response explains itself, never a bare empty list):** when a scanning method — `query`, `search_sessions`, `find_tool_calls`, `find_file_edits`, `list_sessions` — matches nothing, the response carries a `diagnostics` object next to the empty list/count. Shape: `scanned` (one entry per scanned agent — `sessions` count, `date_min`/`date_max`, `source_found`, plus a per-agent `hint` such as `source not found: ~/.pi/agent/sessions` or `source present but contains no sessions`), `corpus` (total sessions + overall date bounds), `filters` (echo of the active filters), `hints` (cause candidates: a `since`/`until` bound that excludes the entire corpus is called out explicitly — e.g. `since='2030-01-01' is after the newest session (…) — the date filter excludes the entire corpus`; otherwise the remaining filters are named, or the result is declared a genuine no-match). Diagnostics are computed only on the empty path — a non-empty response never carries (or pays for) them — and never crash the response (an unreadable source degrades to a per-agent hint).

**`read_session` no longer requires `agent`:** the parameter is optional. When omitted, the id is looked up across every parser (session ids are unique across agents in practice). A rare cross-agent id collision returns `{ambiguous: true, candidates: [...], count}` — a disambiguation list where each candidate carries its `agent`, NOT an error; re-ask with an explicit `agent`. A miss returns `{error: "not_found", agents_scanned: [...]}`. `get_body` was already agent-free (its event id embeds the owning session).

**CLI error contract (a consumer script never sees a Python traceback):** expected failures keep the single `ai-r: <message>` stderr line + non-zero exit (1 generic / 2 ambiguous or invalid / 3 not found); an *unexpected* internal error is emitted as one structured JSON line on stderr (`{"error": "internal_error", "type", "message", "hint"}`) with exit code 1. `AI_R_DEBUG=1` re-raises the original exception for debugging.

<!-- methods:end -->

### Событийное ядро

Вербы выше — новые: одно **событийное ядро** заменяет кучу разовых
инструментов. Каждый парсер читает логи одного агента и выдаёт типизированные
модели, которые нормализуются в единый агент-нейтральный поток —
`user_turn` / `assistant_turn` / `tool_call(...)` / `plan_event`. Небольшой
набор вербов фильтрует, агрегирует и diff-ит этот поток; различия агентов
(`ExitPlanMode` против `update_plan` против `implementation_plan.md`) скрыты
внутри парсеров — вызывающий видит одну форму.

Честно про границу: это **только извлечение сущностей** — реплики, вызовы
инструментов, планы, намерения, реакции. Это **не** граф и **не** хранилище
памяти. Что делать дальше (граф знаний, Obsidian, постоянная память) — уже на
твоей стороне, вне этого репозитория. Полную слоёную схему и список
MCP-инструментов см. в [docs/architecture.md](./docs/architecture.md).

## Быстрый старт (1 команда)

Требования: Python 3.11+ с `venv` или `pip`, и `jq` (для автопатча MCP-конфигов
Claude и Antigravity — остальным `jq` не нужен).

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

Установщик создаёт venv, ставит runtime-пакет, патчит MCP-конфиги для
**Claude**, **Codex**, **OpenCode**, **Antigravity** (где конфиги существуют),
ставит CLI-скилл **Pi** и прогоняет смоук-тесты.

Необязательное дополнение — `tokens`: `AI_R_EXTRAS=tokens bash install.sh`
(или `pip install "ai-r[tokens]"`) добавляет
[tiktoken](https://github.com/openai/tiktoken) — более точные **оценки**
токенов для сессий, чей формат не хранит точных чисел расхода. Полностью
опционально: без него точные числа по-прежнему берутся прямо из файлов сессий
(где записаны), а оценка деградирует до грубой эвристики «символы/4» с честной
пометкой `estimate` — никогда не падение.

Необязательное дополнение — `semantic`: `AI_R_EXTRAS=semantic bash install.sh`
(или `pip install "ai-r[semantic]"` + разовое скачивание модели, которое
установщик делает сам) включает `sort="semantic"` в текстовом поиске (`query`,
`search_sessions`): топ-50 кандидатов от BM25 пересортируются по **смыслу**
локальной многоязычной моделью эмбеддингов (embeddings) —
[intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)
(int8 ONNX, ~118 МБ, MIT), напрямую через
[onnxruntime](https://onnxruntime.ai) + [tokenizers](https://github.com/huggingface/tokenizers) + [numpy](https://numpy.org),
без torch и без постоянного индекса. Почему эта модель: сильный межъязыковой
поиск (русский запрос находит английскую сессию и наоборот) при малом размере.
Как устроен балл, простыми словами: BM25 отбирает 50 лучших совпадений по
словам (это бюджет затрат, а не отсечка по качеству — порога схожести
намеренно НЕТ: у этого семейства моделей даже несвязанные тексты получают
≈0.7); внутри пула итоговый балл = **75 % смысл + 25 % совпадение слов** —
смысл главнее, а словесная доля не даёт утопить точное вхождение термина и
разбивает ничьи. Полностью опционально: без пакетов или файлов модели
`sort="semantic"` честно откатывается к порядку BM25, и ответ объясняет
почему (`semantic: {active: false, reason, fallback: "bm25"}`) — никогда не
падение.

## Границы: читалка, не охранник

- **Только чтение.** Никогда не запускает код агента и не пишет в его историю —
  читает и возвращает.
- **Ни графа, ни памяти.** Достаёт сущности (реплики, вызовы, планы,
  намерения). Строить из них граф знаний или память — твоя задача, не его.
- **Не контроль доступа.** Кто дотянулся до CLI, MCP-сервера или пакета — читает
  любую сессию. Проверки прав перед парсерами нет; держи там, куда чужие
  локальные процессы не дотянутся.
- **Содержимое сессий — данные, не команды.** Кто читает (аудитор,
  суммаризатор), обязан относиться к тексту сессии как к данным, а не
  инструкциям. См. [Безопасность](docs/security.md).

<!-- scenarios:start -->

## Acceptance summary

Full spec: [docs/scenarios.md](docs/scenarios.md) — 83 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

| Function | # scenarios | Headline pass criteria |
|---|---|---|
| `query` | 12 | Facet filters return correct event shape (references, no body inlined — `text` is a ~160-char preview, a real cut flagged `text_truncated: true`, full body via `get_body`); `relative_to`+`direction` walk yields the true prev/next turn (cross-checked vs `read_session`); `text sort=relevance` is BM25-ranked; `tool_call` events carry an `is_error` outcome (cross-agent best-effort) without changing counts; every `tool_call` event carries a wrapper-aware `tool_kind` (`edit\|write\|read\|bash\|task\|skill\|mcp\|web\|other`) and — when the wrapper's input names the real actor — `tool_resolved` (subagent type under Task/Agent/spawn_agent, skill name under Skill/SlashCommand, `server:tool` under `mcp__*`; no signal → no field, never guessed), the `tool_kind` facet filters by it (unknown value fails loud) and the `tool` facet also matches resolved names; session-level `noise=exclude\|include\|only` drops/isolates subagent sessions before any message is read, an unknown mode fails loud; unimplemented facets `kind`/`parent`/`group` **fail loud** ("not yet supported"), never a silent result; session-level `project_dir` filter scopes events to one project (exact-or-descendant, path-boundary aware); the `session` facet accepts a single uuid OR a list of uuids — the union of those sessions' events in one call (duplicates collapse, an unknown uuid contributes nothing, an empty list or non-string item fails loud — never a silent full-corpus scan). |
| `get_body` | 5 | Body fetched on-demand by id (turn text / plan text / codex steps); `shallow=true` on a draft id returns the task's **final** body + `dropped_drafts`; codex plan `steps`/`status` populated; a plan-feedback `ref` (`"<session>:pf<N>"`) resolves to the FULL raw plan response (type `plan_feedback`, redacted, capped), out-of-range/unknown refs are `not_found`. |
| `aggregate` | 5 | `sum(count) == len(rows)`; `rank_by=stats` order is `(-sessions,-edits,label)`; `kind_split=true` adds `kind_split_available`+`note`; empty rows → empty result, no crash; the `tokens` metric folds per-row `tokens` blocks into `{input, output, reasoning, cache_read, cache_write, total, exact, estimated, unknown}` — sums stay `null` when no row carried the field (never a fabricated 0) and `exact + estimated + unknown == len(rows)` always holds; the `component_tokens` metric folds per-row per-component blocks (`user_turn`/`assistant_turn`/`thinking`/`plan`/`tool_call.<kind>`) with `estimated`/`unknown` provenance, a component no row carried staying absent (never a fabricated 0). |
| `diff` | 1 | Edit rows stitch into a per-file unified diff; bodies stay on-demand. |
| `detect_current` | 1 | Returns a sensible runtime identity (`session_id`/`agent`/`candidates`/`verified`). |
| `plan` | 11 | Tasks grouped by plan-file **slug**, not title (drifting titles stay ONE task, zero false `completed_major`); N draft + 1 final by `(ts,seq)`; cross-agent codex `update_plan` normalized; no false positive from a quoted `update_plan`; empty (not error) for agents with no plan signal; F3.4 default schema — the **final** plan's full text inlined (`body` + `body_source`, the user-edited approval text is authoritative over the signal/file body; honest `null` for steps-only plans), drafts stay references, and `feedback` carries ALL «plan quote → user comment» pairs (chronological, `plan_id`-bound, `verdict ∈ rejected\|stay_in_plan_mode`, `quote=null` for free-text comments, raw response on-demand via `ref`); technical failures filtered; agents without an approval flow contribute an honest empty `feedback`; `bodies="none"`/`feedback=false` restore the historical shape; v2 — every atom carries `version` (v1…vN per task, chronological, final = vN), every pair carries `plan_version` + `round` + `section` (the quote anchored to its source-markdown section through markup-stripping normalization — miss or multi-section ambiguity is an honest `null`, never a nearest guess; a rejected plan-file `Write` correlates like an `ExitPlanMode` verdict), and `rounds=all\|last` filters to each session's final feedback round (unknown value fails loud). |
| `session_stats` (preset) | 4 | All 4 dims (agent/dir/date/kind) give sensible counts; degenerate kind split → `kind_split_available=false`+note; **byte-identical** to manual `aggregate(rank_by=stats, kind_split=true)` on a FROZEN snapshot; `with_tokens=true` (F3.3) reads token usage at request time and adds a folded `tokens` block to every group + totals — **exact** where the agent's files record usage (Claude `message.usage` deduped per API call, Codex last cumulative `token_count`, OpenCode `message.data.tokens`, Pi `usage`), a labeled `estimate` otherwise (optional tiktoken, else a rough chars/4 heuristic — degradation, never a crash), honest `unknown` without any signal; default `false` is byte-identical to the historical output. |
| `session_diff` (preset) | 2 | Claude session → per-file hunks in chronological order with intent attached (cross-checked vs `read_session`); codex session reconstructs targets from `printf >`/`cat > <<EOF`, with `tee`/`sed -i`/`cp`/`mv` documented as silently skipped. |
| `incidents` (preset) | 4 | One call finds dangerous shell commands + regret reactions (F4.1) via a baked chain — ONE `query(type=tool_call, tool_kind=bash)` scan → deterministic danger dictionary on the extracted command (a Bash `description` alone never fires; `--force-with-lease` is not force-push) → bilingual (ru+en) regret-marker scan over the next `reaction_window` messages (default 6) — the two-step `confirmed` verdict, never guessed; each record carries the query event `id` (context on-demand via `relative_to`), `patterns`+`categories`, a char-capped `command` fragment centred on the hit, tri-state `is_error` (`null` where the agent's format has no correlated outcome signal) and `reaction` (marker labels + capped preview, `null` when unconfirmed); `count`/`confirmed_count`/`by_pattern` reflect the FULL match set independent of `limit`; `category`/`confirmed` filters fail loud on unknown values; emitted fields are redacted by default while matching runs on RAW text; zero incidents → `diagnostics`; documented dictionary caveat: quoting a dangerous string (echo/grep/test payloads) can still match — mention vs execution is not decidable by regex. |
| `network` (preset) | 4 | One call audits network egress (F4.3) via a baked chain — ONE `query(type=tool_call, tool_kind=web)` scan (Claude `WebFetch`/`WebSearch`, OpenCode `webfetch`, Codex `web_search` surfaced from `web_search_call` rollout records, Gemini/Antigravity `web_fetch`/`google_web_search`; Pi records no web tool — honest absence) → the request target (`url`/`query`) extracted from the call's own input (never guessed from the tool name; no target → honest `null` fields, `kind: null`) → a deterministic **risk dictionary** (`plain_http`, `credentials_in_url`, `secret_in_url`/`secret_in_query` — the redaction patterns double as the detector, `ip_literal_host`, `private_or_local_host`, `punycode_host`); each record carries the query event `id` (context on-demand via `relative_to`), derived `kind` (`fetch`\|`search`), char-capped `url`/`query`, `domain`, `risks` and tri-state `is_error`; `count`/`risky_count`/`by_domain`/`by_risk` reflect the FULL match set independent of `limit`; `kind`/`risk` filters fail loud on unknown values, `domain` matches equals-or-subdomain; risk assessment runs on RAW strings while emitted fields are redacted by default (cap applied AFTER redaction — a boundary-sliced secret never leaks); zero requests → `diagnostics`; documented caveat: MCP-mediated network access stays under `tool_kind="mcp"` — never guessed into the audit. |
| `find_file_edits` | 3 | Default MCP call is **reference-by-default** (`input_sha256`+`input_chars`, NOT full `input`); `include_input=true` restores the body; body otherwise fetched on-demand via `get_body`. |
| `list_sessions` | 5 | Newest-first, paginated (`limit`/`offset`, `truncated` flag) inventory; each summary carries `kind`+`parent_uuid` (subagent detection: Claude/OpenCode/Codex/Pi; Antigravity has no signal); `agent` filter narrows the set; `noise=exclude\|include\|only` splits the inventory into top-level vs subagent sessions and composes with `kind` by AND; the Claude parser merges the CLI transcript root with the Claude Desktop metadata root — dedup by uuid, Desktop title wins (CLI title kept in `extra["cli_title"]`), origin marked `extra["source_root"]="cli"\|"desktop"`, a metadata-only session stays visible as a zero-message reference; each summary carries top-level `project_dir`+`launch_surface` (null when the format has no signal) and `project_dir` filters the inventory exact-or-descendant. |
| `outcome` (read_session field) | 2 | `read_session` carries `outcome` — `status ∈ success\|failure\|mixed\|unknown` from two honest signals: tool-call error rate (real flag only for Claude/OpenCode — `tool_errors`/`error_rate` are `null` elsewhere, `error_rate_reliable` says which) and a calibrated bilingual (ru+en) success/failure dictionary over the last 3 *human* user turns (assistant self-reports never trusted); every deciding reason spelled out in `signals` (empty ⇔ `unknown`); no raw session text in the block; nothing guessed — no signal is `unknown`, never a fabricated verdict. |
| `resume_command` (summary field) | 1 | Every session summary carries `resume_command` — the ready-to-run CLI one-liner (`cd <project_dir> && claude --resume <uuid>` / `codex resume <uuid>` / `opencode --session <id>` / `pi --session <path>`), shell-quoted, `cd`-prefixed when `project_dir` is known; `null` exactly where no real command exists (Antigravity, subagent sessions, reference-only Desktop sessions) — text only, never executed. |
| `find_tool_calls` | 5 | Exact `tool_name` vs substring `tool_name_pattern` search, cross-agent; neither/both arguments **fail loud** (`invalid_argument`), never a silent empty result; each record surfaces the correlated `is_error` outcome + char-capped `output` (authoritative for Claude/OpenCode, best-effort elsewhere) + `is_error_reliable`; each record also carries the wrapper-aware `tool_kind` + `tool_resolved` (the real name under a Skill/Task/MCP wrapper, `null` without a signal); `input_contains`/`output_contains`/`output_excludes`/`is_error` filters compose by AND (domain × error without a special verb); adaptive `output_mode` (`smart` for errors) keeps a trailing error line that `head` would drop. |
| `read_session` | 5 | Reads one session into the compact `{role, content}` projection with metadata + pagination echo; `offset`/`limit` page a stable ordered list, `total` invariant across slices; `agent` is **optional** — an id resolves across every parser, a rare cross-agent id collision returns a `candidates` list (not an error), a miss names `agents_scanned`; `with_tokens=true` (F3.3) attaches `summary.tokens` (flat exact-or-estimate) + `summary.component_tokens` — a per-component estimate over ai-r's existing event taxonomy (`user_turn`/`assistant_turn`/`thinking`/`plan`/`tool_call.<kind>`, `total`, always `source="estimate"`, plan-authoring calls under `plan` not `tool_call`, `total == sum(scalars)+sum(tool_call.values())`, empty transcript → `null`) plus per-message EXACT `tokens` blocks where the format records per-message usage (Claude deduped per API call before pagination, OpenCode, Pi; Codex/Antigravity/user turns carry no key — absent, not null); `include_subagents=true` attaches `summary.subagent_rollup` (parent + one child per spawned subagent via `children_of(parent_uuid)` + folded `total`); CLI `ai-r read --with-tokens` prints a `COMPONENT \| TOKENS \| SOURCE` table; integers-and-labels only → outside redaction; default `false` is byte-identical to the historical output. |
| `search_sessions` | 4 | Title/body/all scope; `AND` default, `OR` widens (`AND ⊆ OR`), negative `-term` excludes, quoted phrase is contiguous; `scope=body` returns a matching `snippet`; BM25 vs date sort; `noise=exclude` removes subagent matches before scanning, `noise=only` searches only the subagent tree. |
| empty-result diagnostics (cross-cutting) | 2 | A zero-result `query`/`search_sessions`/`find_tool_calls`/`find_file_edits`/`list_sessions` response carries `diagnostics` (per-agent scan counts + date bounds + `source_found`, corpus totals, cause hints: missing source dir / all-excluding `since`/`until` / remaining filters); a non-empty response never carries it. |
| secret redaction (cross-cutting) | 3 | Every text-emitting method masks secrets on output as `[REDACTED_<TYPE>]` by default and carries a `redactions` type→count dict; `redact=false` returns the raw content; matching always runs on the RAW stored text (searching a literal secret finds its session, only the display is masked); a `[REDACTED_*]` placeholder or secret-looking filter value on an empty result earns a diagnostics hint suggesting `redact=false`. |
| semantic sort (cross-cutting) | 3 | `sort="semantic"` on the text-search surface (`query` text facet, `search_sessions`) re-ranks the BM25 top-50 candidates by meaning with a local multilingual embedding model (`intfloat/multilingual-e5-small`, int8 ONNX, direct onnxruntime+tokenizers, mandatory `query:`/`passage:` prefixes applied internally, no persistent index); blended candidate score = 75 % meaning + 25 % word match (min–max normalized within the pool), no similarity cut-off — results are re-ordered, never dropped, the tail keeps BM25 order; the response carries a `semantic` report (`active: true` + model/candidates/weight, or `active: false` + plain-words `reason` + `fallback: "bm25"`); without the optional `ai-r[semantic]` deps/model files the order honestly falls back to plain BM25 — never a crash — and the default sorts never touch the module; cross-lingual ru↔en retrieval works both ways. |
| CLI error contract | 1 | A failing `ai-r` CLI invocation exits non-zero with a structured error on stderr (single `ai-r: …` line, or one JSON `internal_error` line for unexpected failures) — never a Python traceback; `AI_R_DEBUG=1` re-raises for debugging. |

<!-- scenarios:end -->

## Дальше — документация

- Словарь методов (вербы + пресеты) — [`docs/methods.md`](./docs/methods.md)
  (англ. SSOT) · [`docs/methods.ru.md`](./docs/methods.ru.md) (рус. зеркало)
- Приёмочные сценарии (32 e2e) — [`docs/scenarios.md`](./docs/scenarios.md)
- Архитектура и слои — [`docs/architecture.md`](./docs/architecture.md)
- Операторы поиска — [`docs/search-operators.md`](./docs/search-operators.md)
- Регистрация MCP по агентам — [`docs/mcp-registration.md`](./docs/mcp-registration.md)
- Покрытие парсеров и ограничения — [`docs/parsers.md`](./docs/parsers.md)
- Безопасность (недоверенное содержимое) — [`docs/security.md`](./docs/security.md)
- Добавить шестого агента — [`CONTRIBUTING.md`](./CONTRIBUTING.md)

## Разработка

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ тестов, CI требует покрытие ≥80%
- Conventional Commits (`feat:`, `fix:`, `docs:`, …)
- Про добавление новых агентов см. [CONTRIBUTING.md](./CONTRIBUTING.md) и
  [docs/parsers.md](./docs/parsers.md)

<details>
<summary>Ключевые слова</summary>

claude code session reader · claude code session parser · codex session parser ·
opencode session reader · antigravity brain parser · pi agent session reader ·
cross-agent attribution · ai coding agent audit · ai agent session history ·
mcp session tools · read-only session reader · agent session replay ·
resume agent session · agent handoff · plan extraction · tool-call audit ·
file edit attribution · multi-agent coding · claude codex opencode antigravity pi

</details>

## Лицензия

MIT — см. [LICENSE](./LICENSE).

---

**Начать:** clone + `bash install.sh`, затем зарегистрируйте MCP-сервер для
своего агента ([docs/mcp-registration.md](./docs/mcp-registration.md)) и
перезапустите host-инструмент. Одна read-only поверхность к истории каждого
агента.
