# ai-r — словарь методов (русское зеркало)

> Русский перевод-зеркало английского SSOT `docs/methods.md`. README.ru фреймит этот файл (маркер-блок). Держать синхронным с `docs/methods.md` при каждой смене функционала.
>
> **Статус:** Phase 1–3b живые. Событийное ядро `query` + пресеты `intent`/`reaction`, `plan` + `get_body`, вербы `aggregate`/`diff`/`detect_current` (`src/ai_r/events.py`). **Phase 3b:** вербы обогащены (`query(with_intent)`, `aggregate(rank_by, kind_split)`, `diff` над intent-несущими rows) → **`session_stats` и `session_diff` теперь тонкие пресеты над вербами с доказанной byte-parity на РЕАЛЬНЫХ данных** (frozen-snapshot ~/.claude: session_stats 8/8 group_by×top EQUAL; session_diff 12/12 сессий EQUAL). Parity-тесты `tests/test_phase3b_parity.py` + весь legacy-сьют зелёный. `find_file_edits`/`find_tool_calls`/`search_sessions`/`detect-*` остаются отдельными (обоснование ниже). Фасеты `kind=subagent`/`parent` в `query` — заглушки (Phase 3).
>
> **Инвариант MCP-поверхности:** 13 тулов = 7 legacy + 5 event-core вербов + 1 пресет (`plan`).
> Источник истины — код (`@mcp.tool()` в `mcp_server.py`); стережёт `tests/test_docs_sync.py`.

<!-- methods:start -->

## Verbs

| verb | назначение | параметры |
|---|---|---|
| `query` | фильтр/поиск событий сессий; `with_intent=True` → на каждое событие top-level `intent` (та же `previous_user_intent`, что у legacy) | type, agent, session, since, until, file, tool, text, sort(relevance\|date), relative_to+direction(prev\|next)+n(1\|all), step_type, limit, with_intent; kind/parent/group — заглушки (Phase 3) |
| `plan` | нормализованные plan-атомы сессии (final vs drafts, группировка по задачам) | session, kind(draft\|final\|completed_major), group=task, agent |
| `get_body` | тело on-demand по id события/плана | id, shallow |
| `aggregate` | rollup над rows (query/find_file_edits/session-inventory) → `{groups, totals}`; `rank_by=stats` даёт session_stats-порядок (sessions→edits→label), `kind_split=True` добавляет `kind_split_available`/`note` | rows, group_by(field\|callable), metrics ⊆ count\|sessions\|edits\|intents\|agents\|messages\|files, rank_by(default\|stats), kind_split |
| `diff` | стичинг edit-rows в per-file unified diff (тела on-demand через message_index; `intent` берётся из row при `query(with_intent)`) → `{files:[{file,edits,diff,hunks}], count, caveats}` | rows, per_file=True, format=unified |
| `detect_current` | runtime-identity (env/fs, вне session-query) → `{session_id, agent, candidates[], verified, self}` | agent (hint) |

## Presets (пресеты)

| preset | раскрытие |
|---|---|
| `intent(event, n)` | `query(relative_to=event, direction=prev, n)` |
| `reaction(event, n)` | `query(relative_to=event, direction=next, n)` |
| `plan(session, kind, group=task)` | `query(type=plan_event, …)` → normalized + kind-tagged (final/draft/completed_major) |
| `session_stats(group_by)` | строит per-session inventory-rows → `aggregate(rows, group_by, rank_by=stats, kind_split=True)` → проекция на legacy-форму totals |
| `session_diff(uuid, agent≠codex)` | `diff(query(type=edit\|write, session=uuid, with_intent=True) c file-ref))` → проекция (без file-level `hunks`) |

## Legacy-тулы: пресеты над вербами (Phase 3b)

Phase 3b обогатила вербы, чтобы старые тулы стали тонкими пресетами **с byte-identical выходом, доказанным на РЕАЛЬНЫХ данных** (frozen snapshot `~/.claude`, чтобы живой vault не мутировал в середине прогона — это давало ложные mismatch'и). Legacy-сьюты (`test_session_stats`/`test_session_diff`) зелёные — вторая половина доказательства совместимости.

**Переведены на вербы (byte-parity доказана):**

| тул | пресет над вербом | доказательство |
|---|---|---|
| `session_stats` | `aggregate(rank_by=stats, kind_split=True)` над per-session inventory-rows | 8/8 (group_by∈agent\|dir\|date\|kind × top∈8\|0) EQUAL на snapshot; ключ — `rank_by=stats` воспроизводит sessions-first ранк, `kind_split` даёт `kind_split_available`/`note` |
| `session_diff` (≠codex) | `diff(query(edit\|write, with_intent=True))` | 12/12 реальных Claude-сессий EQUAL; ключ — `with_intent` возвращает `intent`, единый chronological stream даёт тот же file-order, фильтр edit\|write исключает `Read` (иначе лишние файлы) |

**Codex — исключение в `session_diff`:** codex пишет файлы через shell-exec, target восстанавливается сканом командной строки, которого событийный поток НЕ делает → shell-redirect-правки исчезли бы из `query`-fold. Поэтому codex-ветка `session_diff` сохраняет legacy `_scan_session` (byte-parity для всех агентов).

**Остаются отдельными (обоснованно):**

| тул | почему НЕ пресет |
|---|---|
| `find_file_edits` / `find_tool_calls` | запись несёт `session_title`/`session_date`/`assistant`/`input`, которых НЕТ в событии `query`; воспроизвести их = заново читать сессию (не *тонкий* пресет, а второй парс поверх событий — строго медленнее) + потеря codex shell-redirect-правок. `intent` теперь воспроизводим (`with_intent`), но остальных полей — нет. SSOT богатой edit/tool-записи |
| `search_sessions` | session-гранулярный + BM25-сниппеты сессий; `query` event-гранулярный (turn/tool) → чистого 1:1 нет |
| `detect-agent`/`detect-session` (CLI) | CLI печатает `source` агента и 6 режимов вывода (list/first/strict/self/fingerprint/`--json`/`--count`) + WARN-строку; дикт `detect_current` этого не даёт |

## Plan-атом (нормализованный, различия агентов скрыты)

`Plan { id, session_id, agent, title, task_id, kind: draft\|final\|completed_major, path?, steps?, status?, refs[], sha256 }`. Тело/steps — on-demand через `get_body(id, shallow?)`. `shallow=True` → только final задачи, тела draft-черновиков отброшены (сценарий S6).

**Группировка по задачам = `task_id` (стабильный ключ):** для Claude это slug плана `plans/<slug>.md` (Write несёт path напрямую; `ExitPlanMode` без path наследует slug ближайшего предшествующего plan-Write в сессии; если slug'а ещё не было — fallback на нормализованный title). Для Antigravity — путь `implementation_plan.md`. Для Codex (файла нет) — нормализованный title (непрерывный ран `update_plan`). Ключ по slug'у, а НЕ по title, потому что title дрейфует внутри одной итерации-цепочки (декорации меняют заголовок) — на реальных данных это резало одну задачу на несколько. В группе последний plan_event по (ts, seq) = `final`, ранние = `draft`; строго более ранние задачи (ДРУГОЙ slug) = `completed_major`. Внутренняя таблица parser→сигнал (`ExitPlanMode`/`Write plans/*.md`/`update_plan`/`implementation_plan.md`) — деталь реализации, наружу невидима.

<!-- methods:end -->
