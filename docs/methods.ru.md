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
| `query` | фильтр/поиск событий сессий; `with_intent=True` → на каждое событие top-level `intent` (та же `previous_user_intent`, что у legacy); событие `tool_call` несёт ref `is_error` (исход вызова), когда его результат коррелируется (см. *Границы вывода и исход* ниже) | type, agent, session, since, until, file, tool, text, sort(relevance\|date), relative_to+direction(prev\|next)+n(1\|all), step_type, limit, with_intent; kind/parent/group — заглушки (Phase 3) |
| `plan` | нормализованные plan-атомы сессии (final vs drafts, группировка по задачам) | session, kind(draft\|final\|completed_major), group=task, agent |
| `get_body` | тело on-demand по id события/плана; возвращаемое тело/текст ограничено `max_chars` (по умолчанию 500k) → превышение режется с маркером и помечается `body_truncated` | id, shallow, max_chars |
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
| `find_file_edits` / `find_tool_calls` | запись несёт `session_title`/`session_date`/`assistant`/`input`, которых НЕТ в событии `query`; `find_tool_calls` дополнительно несёт per-record `is_error` (коррелированный исход tool-вызова) и `output` (коррелированное содержимое tool-result, с char-cap); воспроизвести их = заново читать сессию (не *тонкий* пресет, а второй парс поверх событий — строго медленнее) + потеря codex shell-redirect-правок. `intent` теперь воспроизводим (`with_intent`), но остальных полей — нет. SSOT богатой edit/tool-записи |
| `search_sessions` | session-гранулярный + BM25-сниппеты сессий; `query` event-гранулярный (turn/tool) → чистого 1:1 нет |
| `detect-agent`/`detect-session` (CLI) | CLI печатает `source` агента и 6 режимов вывода (list/first/strict/self/fingerprint/`--json`/`--count`) + WARN-строку; дикт `detect_current` этого не даёт |

## Plan-атом (нормализованный, различия агентов скрыты)

`Plan { id, session_id, agent, title, task_id, kind: draft\|final\|completed_major, path?, steps?, status?, refs[], sha256 }`. Тело/steps — on-demand через `get_body(id, shallow?)`. `shallow=True` → только final задачи, тела draft-черновиков отброшены (сценарий S6).

**Группировка по задачам = `task_id` (стабильный ключ):** для Claude это slug плана `plans/<slug>.md` (Write несёт path напрямую; `ExitPlanMode` без path наследует slug ближайшего предшествующего plan-Write в сессии; если slug'а ещё не было — fallback на нормализованный title). Для Antigravity — путь `implementation_plan.md`. Для Codex (файла нет) — нормализованный title (непрерывный ран `update_plan`). Ключ по slug'у, а НЕ по title, потому что title дрейфует внутри одной итерации-цепочки (декорации меняют заголовок) — на реальных данных это резало одну задачу на несколько. В группе последний plan_event по (ts, seq) = `final`, ранние = `draft`; строго более ранние задачи (ДРУГОЙ slug) = `completed_major`. Внутренняя таблица parser→сигнал (`ExitPlanMode`/`Write plans/*.md`/`update_plan`/`implementation_plan.md`) — деталь реализации, наружу невидима.

## Границы вывода и исход tool-вызова

**Ограниченный вывод (untrusted-сессии бывают огромными — поверхность никогда не отдаёт неограниченные байты):** `find_tool_calls` режет у каждой записи поля `input`/`assistant`/`intent`/`output` (превышение обрезается маркером `…[truncated]` и перечисляется в per-record `truncated_fields`) и прекращает добавлять записи при достижении общего байт-бюджета ответа, выставляя `output_truncated`; это отдельно от count-based `truncated` (есть ещё записи). `get_body` ограничивает тело через `max_chars` (`body_truncated`). Tool-input больше 1 МБ никогда не JSON-декодится (возвращается как есть) — общий guard и на событийном потоке, и в `find_tool_calls`. `read_session` рендерит результат вызова как `[tool_result ok: <snippet>]` или `[tool_result ERROR: <snippet>]` (был голый `[tool_result]`).

**Адаптивная обрезка вывода (`output_mode`):** cap на поле `output` каждой записи — `_OUTPUT_CHARS_CAP = 2000` символов. Как тратится этот бюджет, задаёт `output_mode ∈ {"head", "tail", "smart"}`. Дефолт (`output_mode=None`) — **адаптивно per record**: запись с `is_error == True` обрезается по `"smart"` (surface строк-ошибок — `error`/`fatal`/`traceback`/… — плюс хвост, чтобы ошибка в *конце* длинного лога не терялась при head-обрезке), а успешная запись — по `"head"` (legacy-поведение). Явный `output_mode` форсит одну стратегию для всех записей. `smart`/`tail` могут вернуть до ~2× cap, чтобы уместить и поднятые строки, и хвост; при любой обрезке `output` по-прежнему перечисляется в `truncated_fields` этой записи.

**Фильтрация `find_tool_calls` (все опциональны, композируются по И):** помимо `tool_name`/`tool_name_pattern` записи сужаются через `input_contains` (case-insensitive substring по сериализованному tool-input / тексту команды), `output_contains` (ci substring по коррелированному `output`), `output_excludes` (отбросить запись, чей `output` содержит маркер — заданный вызывающим фильтр шума, напр. строка harness security-гейта, `"user rejected"`, `"MANUAL COMMIT BLOCKED"`; **такого списка в ядре НЕ зашито**) и `is_error` (tri-state: `None` = все, `True` = только ошибки, `False` = только успех). Все фильтры пересекаются (И). Отдельного verb'а «ошибка + домен» **нет**: эта связка — *композиция*, напр. `find_tool_calls(input_contains="git", is_error=True)` возвращает реальные сбои команд выбранного домена (`git` — лишь пример домена, не спец-случай).

**`is_error` (исход tool-вызова) — cross-agent best-effort:** **Claude** и **OpenCode** несут реальный флаг успех/ошибка (у Claude — `tool_result.is_error`; у OpenCode — `state.status == "error"`). **Codex** и **Pi** не имеют поля ошибки в записях результата → `is_error` всегда `False` (отсутствие флага, не доказательство успеха). **Antigravity** вообще не эмитит tool-result-записей → сигнала исхода нет. Консьюмеры НЕ должны читать cross-agent `is_error=False` как «подтверждённый успех» для Codex/Pi/Antigravity. `find_tool_calls` теперь несёт тот же `is_error` в каждой записи плюс коррелированный `output` (содержимое tool-result, с char-cap) — корреляция по tool_use_id (у Claude `tool_use.id` / у OpenCode `callID`); с той же best-effort-оговоркой (`is_error` авторитетен только для Claude/OpenCode и по умолчанию `False` для Codex/Pi/Antigravity либо когда результат не коррелируется). Чтобы эта честность была машиночитаемой, каждая запись `find_tool_calls` дополнительно несёт `is_error_reliable` (bool): `True` для Claude/OpenCode (значение подкреплено реальным флагом), `False` для Codex/Pi/Antigravity (источника нет → `is_error` всегда `False` и может **недосчитывать** настоящие сбои). Консьюмер, фильтрующий `is_error=True`, должен читать `is_error_reliable`, чтобы понять, означает ли `False` «подтверждённый успех» или лишь «нет сигнала».

<!-- methods:end -->
