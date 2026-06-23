---
name: ai-r
description: >
  Read-only доступ к локальным сессиям AI-агентов (Claude, Codex, OpenCode, Antigravity, Pi)
  через CLI `ai-r`. Читает файлы сессий с диска — без MCP, ничего не меняет.
  Use when: "покажи мои сессии" / "найди сессию где я правил X" / "кто менял этот файл" /
  "read agent sessions" / "list conversations" / "search session history" / "audit past work" /
  "find file edits across sessions" / "экспортируй сессию".
  Для вопросов про прошлые разговоры/работу агентов вместо ручного grep по jsonl.
---

# ai-r

`ai-r` — консольная утилита. Читает **локальные** файлы сессий AI-агентов с диска и
выводит список / содержимое / поиск. **Read-only**: ничего не правит, ничего не отправляет
наружу, MCP не использует. Просто запускается в bash и печатает результат.

> **Если у твоего агента уже зарегистрирован MCP-инструмент `ai-r`** (Claude, Codex,
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
ai-r list                       # все агенты, все сессии
ai-r list --agent pi            # только Pi
ai-r list --agent pi --days 7   # за последние 7 дней
ai-r list --json                # машинно-читаемый вывод
```
Фильтры даты: `--days N`, `--from-date YYYY-MM-DD`, `--to-date YYYY-MM-DD`, `--limit N`.

### read — прочитать одну сессию
```bash
ai-r read <uuid>                         # человеко-читаемый дамп
ai-r read <uuid> --agent pi              # ограничить агентом
ai-r read <uuid> --messages              # + сообщения (обрезаны)
ai-r read <uuid> --json                  # JSON
```
`<uuid>` можно давать префиксом —matched по полному uuid или имени файла.

### search — поиск по сессиям
```bash
ai-r search "auth" --agent pi                       # по заголовкам
ai-r search "ошибка" --scope body --agent pi        # по тексту сообщений
ai-r search "deploy rollback" --scope all           # заголовок ИЛИ тело
ai-r search "fix login" --operator or --json
```
`--scope`: `title` (по умолч.) / `body` (текст + tool-calls) / `all`.
`--operator`: `and` (по умолч.) / `or` / `not`. Префикс `-term` всегда исключает.

### find-file-edits — кто/когда правил файл
```bash
ai-r find-file-edits src/auth.py                    # все агенты
ai-r find-file-edits src/auth.py --agent pi
ai-r find-file-edits src/auth.py --since 2026-06-01 --json
```
Показывает каждое редактирование файла: дата, агент, сессия, tool, краткий intent.

### detect-agent — какой сейчас агент
```bash
ai-r detect-agent            # текущий агент + источник определения
ai-r detect-agent --quiet    # только имя (для скриптов)
```

### detect-session — id текущей сессии
```bash
ai-r detect-session          # кандидат(ы) на id текущей сессии
ai-r detect-session --json
```

### export rounds — сессия в markdown
```bash
ai-r export rounds <uuid> --agent pi                # в stdout
ai-r export rounds <uuid> --output work/CHANGELOG.md --include-round
```

## Типичный сценарий

Пользователь спрашивает «что я делал в Pi на прошлой неделе?»:
```bash
ai-r list --agent pi --days 7          # список → берём uuid
ai-r read <uuid> --messages            # читаем нужную
```

Пользователь спрашивает «кто правил `install.sh`?»:
```bash
ai-r find-file-edits install.sh        # каждое изменение с датой/агентом
```

## Заметки

- **Read-only.** Ничего не пишет, не удаляет, не отправляет. Безопасно.
- **Без MCP.** Это обычный CLI в bash — никаких subprocess-серверов, никакой автозагрузки.
- Пути сессий по умолчанию: `~/.pi/agent/sessions/` (Pi), `~/.claude/`, `~/.codex/sessions/`,
  `~/.local/share/opencode/`, и т.д. — `ai-r` находит их сам.
- Если вывод большой — добавь `--limit N` или фильтр по дате.
- `--json` удобен, когда нужно обработать вывод дальше.
