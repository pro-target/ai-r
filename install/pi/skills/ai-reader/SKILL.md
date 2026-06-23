---
name: ai-reader
description: >
  Read-only доступ к локальным сессиям AI-агентов (Claude, Codex, OpenCode, Antigravity, Pi)
  через CLI `ai-reader`. Читает файлы сессий с диска — без MCP, ничего не меняет.
  Use when: "покажи мои сессии" / "найди сессию где я правил X" / "кто менял этот файл" /
  "read agent sessions" / "list conversations" / "search session history" / "audit past work" /
  "find file edits across sessions" / "экспортируй сессию".
  Для вопросов про прошлые разговоры/работу агентов вместо ручного grep по jsonl.
---

# ai-reader

`ai-reader` — консольная утилита. Читает **локальные** файлы сессий AI-агентов с диска и
выводит список / содержимое / поиск. **Read-only**: ничего не правит, ничего не отправляет
наружу, MCP не использует. Просто запускается в bash и печатает результат.

> **Если у твоего агента уже зарегистрирован MCP-инструмент `ai-reader`** (Claude, Codex,
> OpenCode, Antigravity) — используй его, он первичнее (типизированные вызовы, готовые
> данные). Этот skill — CLI-фоллбэк для агентов **без** MCP (например, Pi).

Поддерживаемые агенты: `claude`, `codex`, `opencode`, `antigravity`, `pi`.

## Когда использовать

✅ **Используй для:**
- «Покажи мои недавние сессии» / «что я делал вчера»
- «Найди сессию, где мы чинили auth»
- «Кто и когда правил файл `src/auth.py`?»
- Прочитать конкретную сессию по uuid
- Экспортировать сессию в markdown

❌ **Не используй для:**
- Правки сессий (read-only — менять нельзя)
- Чтения того, чего нет на диске (только локальные файлы)

## Команды

### list — список сессий
```bash
ai-reader list                       # все агенты, все сессии
ai-reader list --agent pi            # только Pi
ai-reader list --agent pi --days 7   # за последние 7 дней
ai-reader list --json                # машинно-читаемый вывод
```
Фильтры даты: `--days N`, `--from-date YYYY-MM-DD`, `--to-date YYYY-MM-DD`, `--limit N`.

### read — прочитать одну сессию
```bash
ai-reader read <uuid>                         # человеко-читаемый дамп
ai-reader read <uuid> --agent pi              # ограничить агентом
ai-reader read <uuid> --messages              # + сообщения (обрезаны)
ai-reader read <uuid> --json                  # JSON
```
`<uuid>` можно давать префиксом —matched по полному uuid или имени файла.

### search — поиск по сессиям
```bash
ai-reader search "auth" --agent pi                       # по заголовкам
ai-reader search "ошибка" --scope body --agent pi        # по тексту сообщений
ai-reader search "deploy rollback" --scope all           # заголовок ИЛИ тело
ai-reader search "fix login" --operator or --json
```
`--scope`: `title` (по умолч.) / `body` (текст + tool-calls) / `all`.
`--operator`: `and` (по умолч.) / `or` / `not`. Префикс `-term` всегда исключает.

### find-file-edits — кто/когда правил файл
```bash
ai-reader find-file-edits src/auth.py                    # все агенты
ai-reader find-file-edits src/auth.py --agent pi
ai-reader find-file-edits src/auth.py --since 2026-06-01 --json
```
Показывает каждое редактирование файла: дата, агент, сессия, tool, краткий intent.

### detect-agent — какой сейчас агент
```bash
ai-reader detect-agent            # текущий агент + источник определения
ai-reader detect-agent --quiet    # только имя (для скриптов)
```

### detect-session — id текущей сессии
```bash
ai-reader detect-session          # кандидат(ы) на id текущей сессии
ai-reader detect-session --json
```

### export rounds — сессия в markdown
```bash
ai-reader export rounds <uuid> --agent pi                # в stdout
ai-reader export rounds <uuid> --output work/CHANGELOG.md --include-round
```

## Типичный сценарий

Пользователь спрашивает «что я делал в Pi на прошлой неделе?»:
```bash
ai-reader list --agent pi --days 7          # список → берём uuid
ai-reader read <uuid> --messages            # читаем нужную
```

Пользователь спрашивает «кто правил `install.sh`?»:
```bash
ai-reader find-file-edits install.sh        # каждое изменение с датой/агентом
```

## Заметки

- **Read-only.** Ничего не пишет, не удаляет, не отправляет. Безопасно.
- **Без MCP.** Это обычный CLI в bash — никаких subprocess-серверов, никакой автозагрузки.
- Пути сессий по умолчанию: `~/.pi/agent/sessions/` (Pi), `~/.claude/`, `~/.codex/sessions/`,
  `~/.local/share/opencode/`, и т.д. — `ai-reader` находит их сам.
- Если вывод большой — добавь `--limit N` или фильтр по дате.
- `--json` удобен, когда нужно обработать вывод дальше.
