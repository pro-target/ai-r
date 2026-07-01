# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> **Единая read-only поверхность к истории сессий любого AI-агента для кода** —
> Claude, Codex, OpenCode, Antigravity и Pi — через **MCP**, **CLI**
> или **Python SDK**.
>
> Меняйте агентов, не теряя нить · Атрибутируйте любую правку или команду
> тому агенту, что её сделал · Проигрывайте сессию заново · Извлекайте план
> за работой — по всем пяти агентам, один интерфейс.

```bash
# один запрос, все агенты — найти сессию, где всплыл тот самый баг с auth
ai-r search "auth token refresh" --scope body
```

## Боль: пять хранилищ, нет общего взгляда

Каждый AI-агент для кода держит свою историю переписки — в своём месте,
в своём формате:

- **Claude** и **Codex** пишут JSONL,
- **OpenCode** использует БД SQLite,
- **Antigravity** раскидывает «brain»-директории,
- **Pi** пишет JSONL по проектам.

Пять форматов, пять раскладок. Стоит запустить больше одного агента —
работа **дробится по инструментам**. Сменили агента — потеряли нить.
Нельзя спросить «а что *другой* агент уже пробовал?». А когда всплывает
коммит или правка файла, нет прямого ответа, **какой агент это сделал** —
атрибуция размазана по пяти несовместимым логам, каждый надо учить
отдельно.

## Обещание

`ai-r` сводит все пять в **один read-only интерфейс**. Наведите любого
агента — или скрипт, или себя — на любую сессию, неважно, какой инструмент
её записал. Одна форма запроса для каждого агента; различия форматов
нормализуются внутри парсеров.

## Как устроено

```
┌──────────────────────────────────────────────────────────────┐
│ Public API (3 surfaces)                                       │
│   • ai-r        CLI (argparse)                                │
│   • ai-r-mcp    MCP server (stdio JSON-RPC)                   │
│   • from ai_r.parsers import ...   (Python SDK)               │
└──────────────────────────────────────────────────────────────┘
                          ▲
┌──────────────────────────────────────────────────────────────┐
│ Event core: one agent-neutral stream                          │
│   user_turn · assistant_turn · tool_call(edit|write|read|…)   │
│   · plan_event   → filtered/aggregated/diffed by verbs        │
└──────────────────────────────────────────────────────────────┘
                          ▲
┌──────────────────────────────────────────────────────────────┐
│ Per-agent parsers (read-only)                                 │
│   claude · codex · opencode(SQLite) · antigravity · pi        │
└──────────────────────────────────────────────────────────────┘
```

Каждый парсер читает логи одного агента с диска и выдаёт типизированные
модели `Session` и сообщений. Они нормализуются в единый,
агент-нейтральный **событийный поток** — `user_turn` / `assistant_turn` /
`tool_call(...)` / `plan_event` — а небольшой набор **вербов** фильтрует,
агрегирует и diff-ит этот поток. Различия агентов (`ExitPlanMode` против
`update_plan` против `implementation_plan.md`) скрыты внутри парсеров;
вызывающий видит одну форму.

## Доказательство — он читает сессии, которые его и создали

`ai-r` читает те самые сессии, что построили `ai-r`. По **5 агентам** его
рутинно дёргают реальные консьюмеры, живущие поверх parser API:

- **session-summarizer** / `export rounds` — рендерит сессию в
  CHANGELOG-подобный документ-передачу.
- **git-log-auditor** — свежий агент, чья единственная задача — холодно
  проверить, что предыдущий агент реально сделал и решил. Это ловило
  агентов, что втихую вводили планирование в заблуждение.
- **ai-local-reader** — read-only скилл, аудит прошлых сессий с локального
  диска по всем пяти агентам.
- **MCP-регистрации** — сервер автоматически регистрируется в Claude,
  Codex, OpenCode и Antigravity; Pi получает CLI-скилл.

Эти консьюмеры **на стороне воркфлоу** и живут вне этого репозитория; они
вызывают read-only parser API `ai-r` (`read_messages`, MCP-инструменты,
вербы). Сам `ai-r` остаётся читалкой.

## Быстрый старт (1 команда)

Требования: Python 3.11+ с `venv` (`python3-venv`) или `pip`
(`python3-pip`/`pip3`), и `jq` (используется для автопатча MCP-конфигов
Claude и Antigravity — остальным `jq` не нужен).

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

Вот и всё. Установщик:
- По умолчанию per-user режим; режим `opt` — только явно
- Создаёт venv, ставит runtime-пакет
- Патчит MCP-конфиги для **Claude**, **Codex**, **OpenCode**,
  **Antigravity**, если эти файлы конфигов существуют
- Ставит CLI-скилл **Pi** в `~/.agents/skills/ai-r/SKILL.md`, если его нет
- Прогоняет смоук-тесты

## Поддерживаемые агенты

| Агент | Хранилище | Парсер |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite (авто-детект snap/flatpak) |
| Antigravity | `~/.gemini/antigravity/brain/` | JSON / markdown brain-директории |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

Не ваш агент? Добавить шестого — это **один модуль-парсер**; read-only
паттерн портируется на любой инструмент (Cursor, Cline, ваш собственный)
за минуты. См. [CONTRIBUTING.md](./CONTRIBUTING.md).

## Поверхности

`ai-r` даёт одну и ту же силу чтения тремя способами:

- **MCP-сервер** (`ai-r-mcp`) — 13 инструментов через stdio JSON-RPC,
  так что любой MCP-агент дёргает его напрямую (рекомендуется).
- **CLI** (`ai-r`) — субкоманды для скриптов и ручного использования.
- **Python SDK** (`from ai_r.parsers import ...`) — парсеры,
  типизированные модели `Session`/сообщений и событийные вербы, чтобы
  строить свои инструменты.

### Словарь методов (SSOT)

Блок ниже сфреймлен из [`docs/methods.ru.md`](./docs/methods.ru.md) —
русского зеркала первоисточника ([`docs/methods.md`](./docs/methods.md),
англ.) по публичным вербам и пресетам. Держится синхронным с
маркер-блоком того файла.

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

### Что добавляет эта ветка — событийное ядро

Вербы выше — новые: одно **событийное ядро** заменяет кучу разовых
инструментов. Главное:

- **`query`** — рабочая лошадка. Фильтрует единый событийный поток по
  `type` / `agent` / `session` / дате / `file` / `tool` / `text`. При
  `sort="relevance"` текстовое совпадение ранжируется BM25 (тот же скорер,
  что у `search_sessions`). С `relative_to`+`direction`+`n` ходит по
  соседним репликам — примитив за `intent` и `reaction`.
- **пресеты `intent` / `reaction`** — `intent(event)` = запрос юзера
  *за* событием (шаг назад); `reaction(event)` = ответ юзера *после*
  реплики ассистента (шаг вперёд — критика, уточнение, одобрение).
- **`plan`** — нормализованные plan-атомы по сессии, сгруппированные по
  задачам, помеченные `final` / `draft` / `completed_major`. Так можно
  извлечь *план, на котором агент остановился*, против отброшенных
  ревизий — по Claude / Codex / Antigravity, где сигналы плана разные.
  `get_body(..., shallow=True)` отдаёт субагенту только финальный план,
  черновики отброшены.
- **`aggregate` / `diff` / `detect_current`** — общий rollup, per-file
  сшитый diff и runtime-самоидентификация. `session_stats` и
  `session_diff` теперь тонкие пресеты поверх них, с byte-identical
  выходом, доказанным на реальных данных (см. SSOT-блок выше).

Честный скоуп: это **read-only извлечение сущностей** — реплики, вызовы
инструментов, планы, намерения, реакции. Это **не** граф и **не**
хранилище памяти. Что консьюмер делает дальше (расщепляет в граф знаний,
Obsidian, персистентную память) — сознательно **вне скоупа** и живёт на
стороне консьюмера.

### MCP-инструменты

MCP-сервер даёт 13 инструментов. Основа для чтения:

| Инструмент | Назначение |
|---|---|
| `list_sessions(agent?, limit?, offset?)` | Список найденных сессий, опционально по агенту. С пагинацией. |
| `read_session(uuid, agent, offset?, limit?)` | Читает одну сессию; до 100 сообщений по умолчанию, `offset`/`limit` для пагинации. |
| `find_file_edits(path, agent?, since?, until?, limit?)` | Все правки файла по пути, по умолчанию кросс-агентно, опционально за период. |
| `find_tool_calls(tool_name?, tool_name_pattern?, agent?, since?, until?, limit?)` | Все вызовы инструментов — shell, записи файлов, web-fetch, MCP-вызовы — каждый несёт запустивший его запрос юзера как `intent`. |
| `search_sessions(query, agent?, scope?, operator?, limit?, sort?)` | Поиск по заголовку и/или телу с `AND`/`OR`/`NOT` и Google-style `-term`; `sort=relevance` (BM25) или `date`. |
| `session_stats(agent?, since?, until?, group_by?, top?)` | Группирует + ранжирует сессии по `agent`/`dir`/`date`/`kind`. |
| `session_diff(session_uuid, agent, path?)` | Реконструирует, что сессия изменила, per-file, без git. |
| `query`, `plan`, `get_body`, `aggregate`, `diff`, `detect_current` | Вербы событийного ядра, описанные выше. |

**Пагинация** (`limit`/`offset`, плюс флаг `truncated`, когда есть ещё
страницы) доступна на MCP-инструментах и в Python SDK — см.
[architecture.md](docs/architecture.md).

### CLI

```bash
# list / read / search
ai-r list --agent pi
ai-r read --agent pi <session-uuid>
ai-r search "refactor"
ai-r search "pwa manifest" --scope body --operator and --agent claude

# кто правил файл, по всем агентам, опционально за период
ai-r find-file-edits src/auth.py --since 2026-06-01 --until 2026-06-30
ai-r find-file-edits "config" --agent claude --limit 20

# что агенты запускали? точное имя тула или подстрока-паттерн, за период
ai-r find-tool-calls Bash --since 2026-06-01
ai-r find-tool-calls --pattern deploy --agent codex

# какие файлы меняются чаще? ранк по правкам / сессиям / запросам / агентам
ai-r file-frequency --top 10
ai-r file-frequency --path src/ --agent claude --since 2026-06-01

# какой я агент / в какой сессии (скрипты, оркестрация, self-resume)
ai-r detect-agent --quiet          # → напр. "claude"
ai-r detect-session --json         # → кандидаты UUID сессий

# рендер сессии как раунд CHANGELOG (документ-передача / реплей)
ai-r export rounds <session-uuid> --include-round --output round.md
```

Добавьте `--json` к большинству субкоманд для машиночитаемого вывода.
Вербы событийного ядра (`query`/`plan`/`aggregate`/`diff`/`detect_current`)
доступны через MCP и Python SDK; CLI покрывает перечисленные субкоманды.

#### Операторы поиска

`search_sessions` (MCP) и `ai-r search` (CLI) делят один парсер запросов
и параметр оператора. Поведение по умолчанию (`scope="title"`,
`operator="AND"`, `limit=50`) — исторический подстрочный поиск только по
заголовку.

**Синтаксис запроса**

| Форма | Пример | Смысл |
|---|---|---|
| Голые слова | `pwa manifest` | Оба терма (как — решает оператор). |
| Фраза в кавычках | `"exact phrase"` | Один литеральный терм. |
| Негативный префикс | `-claude` | Google-style: этого терма НЕ должно быть. |

Слова `AND`, `OR`, `NOT` внутри запроса — литеральные поисковые термы.
Булево поведение выбирается через `--operator and|or|not` (CLI) или
`operator="AND"|"OR"|"NOT"` (MCP).

**Режимы оператора** (как комбинируются позитивные термы)

| Режим | семантика `pwa manifest` | семантика `pwa -claude` |
|---|---|---|
| `AND` (по умолчанию) | оба должны быть | `pwa` есть, `claude` нет |
| `OR` | хотя бы один есть | один из `pwa` есть, `claude` нет |
| `NOT` | ни одного нет | ни `pwa`, ни `claude` нет |

**Режимы scope**

| Scope | Где идёт поиск |
|---|---|
| `title` (по умолчанию) | только `session.title` — исторический режим заголовка. |
| `body` | текст сообщений + `tool_use[*].input` + `tool_result[*].content` по каждой сессии. |
| `all` | заголовок ИЛИ тело. |

Когда `scope` = `body` или `all` и совпадение найдено, результат включает
поле `snippet` (CLI: печатается в таблице) — первый совпавший фрагмент,
до 200 символов. Результаты по умолчанию ранжируются BM25
(`sort=relevance`); передайте `sort=date` для сортировки по свежести.

**Заметка о производительности**: `body` и `all` вызывают `read_messages`
на каждой кандидатной сессии. На больших vault'ах первый прогон может быть
медленным; поднимайте `--limit`, чтобы держать выборку ограниченной при
итерациях.

**Пример MCP**

```python
search_sessions(
    query='pwa -claude',
    agent='claude',
    scope='body',
    operator='AND',
    limit=20,
)
```

**Примеры CLI**

```bash
# только заголовок (legacy, по-прежнему по умолчанию)
ai-r search "refactor"

# body-поиск, все термы обязательны, исключить claude
ai-r search "pwa manifest -claude" --scope body --operator and

# body-поиск, любой терм, максимум 5 результатов
ai-r search "pwa manifest" --scope body --operator or --limit 5

# всё, что не содержит ни одного из этих термов
ai-r search "auth login" --scope body --operator not
```

### Python SDK

```python
from ai_r.parsers import AgentName, claude

for session in claude.list_sessions():
    print(session.uuid, session.title)

session = claude.read_session("<session-uuid>")
print(session.message_count)

messages = claude.read_messages("<session-uuid>")
print(messages[0].role, messages[0].text)
```

Полную слоёную схему см. в [docs/architecture.md](./docs/architecture.md).

## Сценарии — один job на реального консьюмера

Одна читалка по всем агентам открывает задачи, которых лог одного агента
не может:

- **Кросс-агентная атрибуция — «какой агент это сделал?»** Каждая правка
  пути, каждый вызов инструмента, по всем агентам и сессиям, помечены
  запустившим их запросом. С привязкой к периоду: «что агенты делали с
  `src/auth.py` на прошлой неделе?» — `find-file-edits` /
  `find-tool-calls`. Питает **git-log-auditor**.
- **Аудит и реплей — холодно проверить, что агент реально сделал.** Свежий
  агент читает прошлую сессию и докладывает, что он *запускал*, а не только
  что заявлял. `session_diff` реконструирует per-file изменение без git;
  `export rounds` рендерит CHANGELOG-подобную передачу. Питает
  **session-summarizer** и **ai-local-reader**.
- **Резюм и передача — сменить агента посреди задачи, сохранив нить.**
  Упёрлись в лимит провайдера или кончилось окно контекста? Начните свежую
  сессию (любой агент), отдайте ей UUID предыдущей и продолжайте. Прошлый
  транскрипт читаем независимо от того, какой инструмент его записал —
  `read_session`, `detect-session`.
- **Правки файлов + намерения — почему этот файл всё менялся?**
  `file-frequency` сворачивает, какие файлы churn-ят больше всего,
  ранжируя по правкам, отдельным сессиям, отдельным запросам и вовлечённым
  агентам; каждая правка несёт стоящий за ней запрос юзера как `intent`.
- **Извлечение плана — восстановить план, на котором агент остановился.**
  `plan` возвращает нормализованные plan-атомы по задачам, `final` против
  `draft`, по Claude / Codex / Antigravity. Отдайте субагенту только
  финальный план через `get_body(..., shallow=True)`.

## Отличия от альтернатив

*Проверено через WebSearch, 2026-07-01.* Пространство вьюеров одного
агента переполнено (claude-code-viewer, claude-code-history-viewer,
claude-session-viewer, simonw/claude-code-transcripts, claude-view);
горстка новых инструментов *действительно* кросс-агентна
(jazzyalex/agent-sessions, Dicklesworthstone/coding_agent_session_search,
hacktivist123/agent-session-resume). Чем отличается `ai-r`:

| Возможность | Вьюеры одного агента | Кросс-агентные session-тулы | `ai-r` |
|---|---|---|---|
| Читает логи >1 агента | Нет | Да | Да — Claude, Codex, OpenCode, Antigravity, Pi |
| Программная поверхность | В основном GUI/TUI | В основном TUI/CLI/app | **MCP + CLI + Python SDK** |
| Атрибуция (правка/команда → агент + intent) | — | Частично (у некоторых provenance) | Да — `find-file-edits` / `find-tool-calls`, каждый с `intent` |
| Аудит-реплей (реконструкция изменений сессии, без git) | — | Редко | Да — `session_diff` |
| Извлечение плана (final vs draft, нормализовано) | — | — | Да — `plan` |
| Скоуп | Вьюер | Поиск / резюм / память | **Read-only ядро извлечения** (граф/память отданы консьюмерам) |

Часть кросс-агентных инструментов идёт в *другую* сторону — к
персистентной памяти или слоям координации (напр. `cass_memory_system`,
`mcp_agent_mail`). `ai-r` сознательно останавливается на read-only
извлечении: память и графы — на стороне консьюмера, не вшиты. Где точные
возможности конкурента неясны из публичных доков, таблица выше скорее
занижает, чем переоценивает.

## Границы дизайна — читалка, не охранник

- **Read-only.** `ai-r` никогда не исполняет код агента и никогда не
  пишет в хранилище сессий агента. Читает и возвращает.
- **Нет графа, нет памяти.** Извлекает сущности (реплики, вызовы
  инструментов, планы, намерения). Строить граф знаний или персистентную
  память поверх — работа консьюмера, вне скоупа этого репо.
- **Не слой контроля доступа.** Любой вызывающий, кто дотянулся до CLI,
  MCP-сервера или пакета, может прочитать любую сессию — авторизации перед
  парсерами нет. Держите там, куда недоверенные локальные вызывающие не
  дотянутся.
- **Содержимое сессии недоверенное.** Вызывающий читалку (аудитор,
  суммаризатор, реплей-агент) должен трактовать содержимое сессии как
  *данные, а не инструкции*. См.
  [Безопасность — недоверенное содержимое сессий](docs/security.md).

Специфичные для воркфлоу ревьюеры, сводки и аудиты живут вне этого репо и
потребляют parser API (`read_messages`).

### Известные ограничения

- **Antigravity** — покрытие фикстурами плюс опциональные смоук-тесты на
  реальных данных, когда локальная brain-директория существует.
- **Shell-правки Codex CLI** — `find_file_edits` восстанавливает записи
  файлов codex из shell-команд `exec_command` / `local_shell_call` через
  консервативный quote-aware скан редиректов (`>` / `>>`). Записи через
  `tee` / `sed -i` / `cp` / `mv` / только-heredoc не детектятся;
  структурные правки (`apply_patch` / `write_file`) — всегда.

Полную матрицу покрытия парсеров см. в [docs/parsers.md](docs/parsers.md).

## Регистрация MCP

`ai-r-mcp` — stdio MCP-сервер. Зарегистрируйте один раз на каждый
host-инструмент. Замените `USER` на своё имя пользователя (или уберите
абсолютный путь, если `ai-r-mcp` в вашем `PATH`). **Перезапустите
host-инструмент после правки его конфига** — ни один не подхватывает
изменения MCP на лету.

Сниппеты ниже используют `/home/USER/.local/bin/ai-r-mcp`. Подправьте
путь, если установка живёт в другом месте (`which ai-r-mcp` подскажет).

### Claude Code

Правьте `~/.claude.json` (top-level объект `mcpServers`):

```json
{
  "mcpServers": {
    "ai-r": {
      "type": "stdio",
      "command": "/home/USER/.local/bin/ai-r-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

Для регистрации в одном проекте закоммитьте `.mcp.json` в корне репо
(см. [`.mcp.json`](./.mcp.json)).

### Codex

Правьте `~/.codex/config.toml`:

```toml
[mcp_servers.ai-r]
command = "/home/USER/.local/bin/ai-r-mcp"
args = []
```

### Gemini CLI

Правьте `~/.gemini/settings.json` (объект `mcpServers`):

```json
{
  "mcpServers": {
    "ai-r": {
      "command": "/home/USER/.local/bin/ai-r-mcp",
      "args": [],
      "timeout": 60
    }
  }
}
```

### OpenCode

Правьте `~/.config/opencode/opencode.jsonc` (top-level объект `mcp`).
OpenCode отличается от прочих тремя вещами: `type` = `"local"` (не
`"stdio"`), `command` — единый слитый массив (команда + аргументы вместе),
а ключ env — `"environment"`.

```json
{
  "mcp": {
    "ai-r": {
      "type": "local",
      "command": ["/home/USER/.local/bin/ai-r-mcp"],
      "enabled": true
    }
  }
}
```

### Antigravity

Правьте `~/.gemini/antigravity/mcp_config.json` (объект `mcpServers`). Это
отличается от конфига Gemini CLI выше — Antigravity держит свой MCP-конфиг
под `~/.gemini/antigravity/`.

```json
{
  "mcpServers": {
    "ai-r": {
      "command": "/home/USER/.local/bin/ai-r-mcp",
      "args": []
    }
  }
}
```

### Pi — скилл, не MCP

У Pi (`@earendil-works/pi-coding-agent`) **нет конфига MCP-сервера** для
правки. Он использует модель расширений/скиллов (`pi install <source>`,
`pi config`), а не карту `mcpServers`, поэтому `ai-r-mcp` нельзя
зарегистрировать как in-process MCP-инструмент внутри Pi (а спавн его
in-process нарушил бы дизайн-контракт Pi). Вместо этого
`install/agent-configs.sh` кладёт read-only **CLI-скилл** в
`~/.agents/skills/ai-r/` — директорию, которую Pi и так сканирует. Скилл
учит модель звать CLI `ai-r` из bash-сессии Pi, без спавна MCP. Сессии Pi
также полностью читаемы *самим* `ai-r` через CLI (`ai-r list --agent pi`,
`ai-r read …`) или Python SDK; оба читают файлы `~/.pi/agent/sessions/`
напрямую. Для слэш-команды `/ai-r` поставьте `enableSkillCommands: true`
в `~/.pi/agent/settings.json` (текст скилла работает и при дефолтном
`false`).

### Заметки

- `ai-r-mcp` должен быть в `PATH`, либо используйте абсолютный путь как
  выше.
- Патч JSON-конфигов использует `jq`. Если `jq` нет, регистрации Codex,
  OpenCode и Pi всё равно проходят; конфиги Claude и Antigravity
  пропускаются — поставьте `jq` или зарегистрируйте руками сниппетами выше.
- Перезапустите host-инструмент после правки его конфига.
- Сервер read-only; любой вызывающий, кто до него дотянулся, может
  прочитать любую сессию. См.
  [Границы дизайна](#границы-дизайна--читалка-не-охранник).

<!-- scenarios:start -->

## Acceptance summary

Full spec: [docs/scenarios.md](docs/scenarios.md) — 32 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

| Function | # scenarios | Headline pass criteria |
|---|---|---|
| `query` | 7 | Facet filters return correct event shape (references, no body inlined); `relative_to`+`direction` walk yields the true prev/next turn (cross-checked vs `read_session`); `text sort=relevance` is BM25-ranked; unimplemented facets `kind`/`parent`/`group` **fail loud** ("not yet supported"), never a silent result. |
| `get_body` | 4 | Body fetched on-demand by id (turn text / plan text / codex steps); `shallow=true` on a draft id returns the task's **final** body + `dropped_drafts`; codex plan `steps`/`status` populated. |
| `aggregate` | 4 | `sum(count) == len(rows)`; `rank_by=stats` order is `(-sessions,-edits,label)`; `kind_split=true` adds `kind_split_available`+`note`; empty rows → empty result, no crash. |
| `diff` | 1 | Edit rows stitch into a per-file unified diff; bodies stay on-demand. |
| `detect_current` | 1 | Returns a sensible runtime identity (`session_id`/`agent`/`candidates`/`verified`). |
| `plan` | 5 | Tasks grouped by plan-file **slug**, not title (drifting titles stay ONE task, zero false `completed_major`); N draft + 1 final by `(ts,seq)`; cross-agent codex `update_plan` normalized; no false positive from a quoted `update_plan`; empty (not error) for agents with no plan signal. |
| `session_stats` (preset) | 3 | All 4 dims (agent/dir/date/kind) give sensible counts; degenerate kind split → `kind_split_available=false`+note; **byte-identical** to manual `aggregate(rank_by=stats, kind_split=true)` on a FROZEN snapshot. |
| `session_diff` (preset) | 2 | Claude session → per-file hunks in chronological order with intent attached (cross-checked vs `read_session`); codex session reconstructs targets from `printf >`/`cat > <<EOF`, with `tee`/`sed -i`/`cp`/`mv` documented as silently skipped. |
| `find_file_edits` | 3 | Default MCP call is **reference-by-default** (`input_sha256`+`input_chars`, NOT full `input`); `include_input=true` restores the body; body otherwise fetched on-demand via `get_body`. |
| `list_sessions` | 1 | Newest-first, paginated (`limit`/`offset`, `truncated` flag) inventory; each summary carries `kind`+`parent_uuid`; `agent` filter narrows the set. |
| `find_tool_calls` | 1 | Exact `tool_name` vs substring `tool_name_pattern` search, cross-agent; neither/both arguments **fail loud** (`invalid_argument`), never a silent empty result. |

<!-- scenarios:end -->

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
- `src/ai_r/validators/` и `src/ai_r/templates/` — опциональные
  самостоятельные помощники (валидация markdown session-note), не часть
  CLI или MCP-поверхности.

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

**Начать:** clone + `bash install.sh`, затем зарегистрируйте MCP-сервер
для своего агента ([Claude](#claude-code) · [Codex](#codex) ·
[OpenCode](#opencode) · [Antigravity](#antigravity) · [Pi](#pi--скилл-не-mcp))
и перезапустите host-инструмент. Одна read-only поверхность к истории
каждого агента.
