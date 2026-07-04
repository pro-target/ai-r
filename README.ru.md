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

| verb | назначение | параметры |
|---|---|---|
| `query` | фильтр/поиск событий сессий; `with_intent=True` → на каждое событие top-level `intent` (та же `previous_user_intent`, что у legacy); событие `tool_call` несёт ref `is_error` (исход вызова), когда его результат коррелируется (см. *Границы вывода и исход* ниже) | type, agent, session, since, until, file, tool, text, sort(relevance\|date), relative_to+direction(prev\|next)+n(1\|all), step_type, limit, with_intent, noise(include\|exclude\|only); kind/parent/group — заглушки (Phase 3) |
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

## Фильтр шума (session-уровень)

Сессия — *шум*, когда это не верхнеуровневый диалог с человеком: сегодня это **порождённые сабагент-сессии (sidechain)** — `kind == "subagent"` или задан `parent_uuid` (SSOT критерия: `src/ai_r/parsers/_noise.py`). `query`, `list_sessions` и `search_sessions` принимают `noise ∈ {include, exclude, only}` (по умолчанию `include` — полная обратная совместимость): `exclude` оставляет только верхнеуровневые agent-сессии, `only` — только дерево сабагентов (аудит-вид). Фильтр применяется на уровне *сессии* до чтения сообщений (исключённая сессия ничего не стоит), композируется с остальными фильтрами по И (включая `list_sessions(kind=…)`), неизвестный режим падает громко (`invalid_argument`). В `query` он игнорируется на прогулке `relative_to` (якорь фиксирует одну конкретную сессию), как и любой другой фасет.

**Покрытие детекции сабагентов (внутренняя нормализация парсеров, один публичный критерий):** **Claude** — layout каталога `subagents/` + sidechain-поле `parentUuid`; **OpenCode** — `session.parent_id`; **Codex** — `session_meta.payload.thread_source == "subagent"` + `parent_thread_id` (включая fallback на вложенный `source.subagent.thread_spawn.parent_thread_id`); **Pi** — заголовочное поле `parentSession`. **Antigravity** — в формате нет сигнала о родителе → всегда `kind="agent"`, шумом не бывает. Warmup/заготовки шумом **не** считаются: ни один формат агента не несёт для них надёжного дешёвого маркера, а эвристика по заголовку промахивалась бы — критерий остаётся точным (шум == сабагент), а не угаданным.

## Границы вывода и исход tool-вызова

**Ограниченный вывод (untrusted-сессии бывают огромными — поверхность никогда не отдаёт неограниченные байты):** `find_tool_calls` режет у каждой записи поля `input`/`assistant`/`intent`/`output` (превышение обрезается маркером `…[truncated]` и перечисляется в per-record `truncated_fields`) и прекращает добавлять записи при достижении общего байт-бюджета ответа, выставляя `output_truncated`; это отдельно от count-based `truncated` (есть ещё записи). `get_body` ограничивает тело через `max_chars` (`body_truncated`). Tool-input больше 1 МБ никогда не JSON-декодится (возвращается как есть) — общий guard и на событийном потоке, и в `find_tool_calls`. `read_session` рендерит результат вызова как `[tool_result ok: <snippet>]` или `[tool_result ERROR: <snippet>]` (был голый `[tool_result]`).

**Адаптивная обрезка вывода (`output_mode`):** cap на поле `output` каждой записи — `_OUTPUT_CHARS_CAP = 2000` символов. Как тратится этот бюджет, задаёт `output_mode ∈ {"head", "tail", "smart"}`. Дефолт (`output_mode=None`) — **адаптивно per record**: запись с `is_error == True` обрезается по `"smart"` (surface строк-ошибок — `error`/`fatal`/`traceback`/… — плюс хвост, чтобы ошибка в *конце* длинного лога не терялась при head-обрезке), а успешная запись — по `"head"` (legacy-поведение). Явный `output_mode` форсит одну стратегию для всех записей. `smart`/`tail` могут вернуть до ~2× cap, чтобы уместить и поднятые строки, и хвост; при любой обрезке `output` по-прежнему перечисляется в `truncated_fields` этой записи.

**Фильтрация `find_tool_calls` (все опциональны, композируются по И):** помимо `tool_name`/`tool_name_pattern` записи сужаются через `input_contains` (case-insensitive substring по сериализованному tool-input / тексту команды), `output_contains` (ci substring по коррелированному `output`), `output_excludes` (отбросить запись, чей `output` содержит маркер — заданный вызывающим фильтр шума, напр. строка harness security-гейта, `"user rejected"`, `"MANUAL COMMIT BLOCKED"`; **такого списка в ядре НЕ зашито**) и `is_error` (tri-state: `None` = все, `True` = только ошибки, `False` = только успех). Все фильтры пересекаются (И). Отдельного verb'а «ошибка + домен» **нет**: эта связка — *композиция*, напр. `find_tool_calls(input_contains="git", is_error=True)` возвращает реальные сбои команд выбранного домена (`git` — лишь пример домена, не спец-случай).

**`is_error` (исход tool-вызова) — cross-agent best-effort:** **Claude** и **OpenCode** несут реальный флаг успех/ошибка (у Claude — `tool_result.is_error`; у OpenCode — `state.status == "error"`). **Codex** и **Pi** не имеют поля ошибки в записях результата → `is_error` всегда `False` (отсутствие флага, не доказательство успеха). **Antigravity** вообще не эмитит tool-result-записей → сигнала исхода нет. Консьюмеры НЕ должны читать cross-agent `is_error=False` как «подтверждённый успех» для Codex/Pi/Antigravity. `find_tool_calls` теперь несёт тот же `is_error` в каждой записи плюс коррелированный `output` (содержимое tool-result, с char-cap) — корреляция по tool_use_id (у Claude `tool_use.id` / у OpenCode `callID`); с той же best-effort-оговоркой (`is_error` авторитетен только для Claude/OpenCode и по умолчанию `False` для Codex/Pi/Antigravity либо когда результат не коррелируется). Чтобы эта честность была машиночитаемой, каждая запись `find_tool_calls` дополнительно несёт `is_error_reliable` (bool): `True` для Claude/OpenCode (значение подкреплено реальным флагом), `False` для Codex/Pi/Antigravity (источника нет → `is_error` всегда `False` и может **недосчитывать** настоящие сбои). Консьюмер, фильтрующий `is_error=True`, должен читать `is_error_reliable`, чтобы понять, означает ли `False` «подтверждённый успех» или лишь «нет сигнала».

## Пустые результаты и поиск сессии

**Диагностика пустого результата (ответ с нулём результатов объясняет себя сам, а не голый пустой список):** когда сканирующий метод — `query`, `search_sessions`, `find_tool_calls`, `find_file_edits`, `list_sessions` — ничего не находит, ответ несёт объект `diagnostics` рядом с пустым списком/счётчиком. Форма: `scanned` (по записи на каждый просканированный агент — счётчик `sessions`, `date_min`/`date_max`, `source_found`, плюс per-agent `hint`, например `source not found: ~/.pi/agent/sessions` или `source present but contains no sessions`), `corpus` (всего сессий + общие границы дат), `filters` (эхо активных фильтров), `hints` (кандидаты-причины: граница `since`/`until`, отсёкшая весь корпус, называется явно — напр. `since='2030-01-01' is after the newest session (…) — the date filter excludes the entire corpus`; иначе перечисляются оставшиеся фильтры либо результат объявляется настоящим no-match). Диагностика считается только на пустом пути — непустой ответ её не несёт (и не платит за неё) — и никогда не роняет ответ (нечитаемый источник деградирует в per-agent hint).

**`read_session` больше не требует `agent`:** параметр опционален. Без него id ищется по всем парсерам (id сессий на практике уникальны между агентами). Редкая коллизия id между агентами возвращает `{ambiguous: true, candidates: [...], count}` — список для развязки (disambiguation), где каждый кандидат несёт свой `agent`, НЕ ошибку; повторите вызов с явным `agent`. Промах возвращает `{error: "not_found", agents_scanned: [...]}`. `get_body` и так был agent-free (его event id содержит сессию-владельца).

**Контракт ошибок CLI (скрипт-потребитель никогда не видит трейсбек питона):** ожидаемые сбои сохраняют одну строку `ai-r: <message>` на stderr + ненулевой exit (1 общий / 2 неоднозначность или неверный аргумент / 3 не найдено); *неожиданная* внутренняя ошибка выдаётся одной структурированной JSON-строкой на stderr (`{"error": "internal_error", "type", "message", "hint"}`) с exit-кодом 1. `AI_R_DEBUG=1` перебрасывает исходное исключение для отладки.

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

Full spec: [docs/scenarios.md](docs/scenarios.md) — 48 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

| Function | # scenarios | Headline pass criteria |
|---|---|---|
| `query` | 9 | Facet filters return correct event shape (references, no body inlined); `relative_to`+`direction` walk yields the true prev/next turn (cross-checked vs `read_session`); `text sort=relevance` is BM25-ranked; `tool_call` events carry an `is_error` outcome (cross-agent best-effort) without changing counts; session-level `noise=exclude\|include\|only` drops/isolates subagent sessions before any message is read, an unknown mode fails loud; unimplemented facets `kind`/`parent`/`group` **fail loud** ("not yet supported"), never a silent result. |
| `get_body` | 4 | Body fetched on-demand by id (turn text / plan text / codex steps); `shallow=true` on a draft id returns the task's **final** body + `dropped_drafts`; codex plan `steps`/`status` populated. |
| `aggregate` | 4 | `sum(count) == len(rows)`; `rank_by=stats` order is `(-sessions,-edits,label)`; `kind_split=true` adds `kind_split_available`+`note`; empty rows → empty result, no crash. |
| `diff` | 1 | Edit rows stitch into a per-file unified diff; bodies stay on-demand. |
| `detect_current` | 1 | Returns a sensible runtime identity (`session_id`/`agent`/`candidates`/`verified`). |
| `plan` | 5 | Tasks grouped by plan-file **slug**, not title (drifting titles stay ONE task, zero false `completed_major`); N draft + 1 final by `(ts,seq)`; cross-agent codex `update_plan` normalized; no false positive from a quoted `update_plan`; empty (not error) for agents with no plan signal. |
| `session_stats` (preset) | 3 | All 4 dims (agent/dir/date/kind) give sensible counts; degenerate kind split → `kind_split_available=false`+note; **byte-identical** to manual `aggregate(rank_by=stats, kind_split=true)` on a FROZEN snapshot. |
| `session_diff` (preset) | 2 | Claude session → per-file hunks in chronological order with intent attached (cross-checked vs `read_session`); codex session reconstructs targets from `printf >`/`cat > <<EOF`, with `tee`/`sed -i`/`cp`/`mv` documented as silently skipped. |
| `find_file_edits` | 3 | Default MCP call is **reference-by-default** (`input_sha256`+`input_chars`, NOT full `input`); `include_input=true` restores the body; body otherwise fetched on-demand via `get_body`. |
| `list_sessions` | 2 | Newest-first, paginated (`limit`/`offset`, `truncated` flag) inventory; each summary carries `kind`+`parent_uuid` (subagent detection: Claude/OpenCode/Codex/Pi; Antigravity has no signal); `agent` filter narrows the set; `noise=exclude\|include\|only` splits the inventory into top-level vs subagent sessions and composes with `kind` by AND. |
| `find_tool_calls` | 4 | Exact `tool_name` vs substring `tool_name_pattern` search, cross-agent; neither/both arguments **fail loud** (`invalid_argument`), never a silent empty result; each record surfaces the correlated `is_error` outcome + char-capped `output` (authoritative for Claude/OpenCode, best-effort elsewhere) + `is_error_reliable`; `input_contains`/`output_contains`/`output_excludes`/`is_error` filters compose by AND (domain × error without a special verb); adaptive `output_mode` (`smart` for errors) keeps a trailing error line that `head` would drop. |
| `read_session` | 3 | Reads one session into the compact `{role, content}` projection with metadata + pagination echo; `offset`/`limit` page a stable ordered list, `total` invariant across slices; `agent` is **optional** — an id resolves across every parser, a rare cross-agent id collision returns a `candidates` list (not an error), a miss names `agents_scanned`. |
| `search_sessions` | 4 | Title/body/all scope; `AND` default, `OR` widens (`AND ⊆ OR`), negative `-term` excludes, quoted phrase is contiguous; `scope=body` returns a matching `snippet`; BM25 vs date sort; `noise=exclude` removes subagent matches before scanning, `noise=only` searches only the subagent tree. |
| empty-result diagnostics (cross-cutting) | 2 | A zero-result `query`/`search_sessions`/`find_tool_calls`/`find_file_edits`/`list_sessions` response carries `diagnostics` (per-agent scan counts + date bounds + `source_found`, corpus totals, cause hints: missing source dir / all-excluding `since`/`until` / remaining filters); a non-empty response never carries it. |
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
