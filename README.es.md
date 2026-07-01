# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> **Una única superficie de solo lectura para el historial de sesiones de todo
> agente de programación con IA** — Claude, Codex, OpenCode, Antigravity y Pi —
> vía **MCP**, un **CLI** o un **SDK de Python**.
>
> Cambia de agente sin perder el hilo · Atribuye cualquier edición o comando al
> agente que lo ejecutó · Reproduce una sesión · Extrae el plan detrás del
> trabajo — a lo largo de los cinco agentes, una sola interfaz.

```bash
# one query, every agent — find the session where that auth bug came up
ai-r search "auth token refresh" --scope body
```

## El dolor: cinco silos, ninguna vista compartida

Cada agente de programación con IA guarda su propio historial de conversación —
en su propio lugar, en su propio formato:

- **Claude** y **Codex** escriben JSONL,
- **OpenCode** usa una base de datos SQLite,
- **Antigravity** dispersa directorios "brain",
- **Pi** escribe JSONL por proyecto.

Cinco formatos, cinco disposiciones. Así que en cuanto ejecutas más de un
agente, tu trabajo queda **aislado por herramienta**. Cambias de agente y
pierdes el hilo. No puedes preguntar «¿qué ya intentó el *otro* agente?». Y
cuando aparece un commit o una edición de archivo, no hay una respuesta directa
a **qué agente lo hizo realmente** — la atribución vive en cinco registros
incompatibles que tendrías que aprender uno por uno.

## La promesa

`ai-r` colapsa los cinco en **una única interfaz de solo lectura**. Apunta
cualquier agente — o un script, o a ti mismo — a cualquier sesión, sin importar
qué herramienta la escribió. La misma forma de consulta para todos los agentes;
las diferencias entre formatos se normalizan dentro de los parsers.

## Cómo funciona

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

Cada parser lee los registros en disco de un agente y emite modelos tipados
`Session` y de mensajes. Estos se normalizan en un único **flujo de eventos**
agnóstico al agente — `user_turn` / `assistant_turn` / `tool_call(...)` /
`plan_event` — y un pequeño conjunto de **verbos** filtra, agrega y diferencia
ese flujo. Las diferencias entre agentes (`ExitPlanMode` vs `update_plan` vs
`implementation_plan.md`) quedan ocultas dentro de los parsers; los llamadores
ven una sola forma.

## Prueba — lee las sesiones que lo construyeron

`ai-r` lee las mismísimas sesiones que construyeron `ai-r`. A lo largo de
**5 agentes** es llamado de forma rutinaria por consumidores reales que viven
sobre la API del parser:

- **session-summarizer** / `export rounds` — renderiza una sesión en un
  documento de traspaso estilo CHANGELOG.
- **git-log-auditor** — un agente nuevo cuyo único trabajo es revisar fríamente
  lo que un agente anterior realmente hizo y decidió. Esto ha pillado agentes
  que desviaron la planificación de forma silenciosa.
- **ai-local-reader** — una skill de solo lectura que audita sesiones pasadas
  desde el disco local en los cinco agentes.
- **Registros MCP** — el servidor se registra automáticamente en Claude, Codex,
  OpenCode y Antigravity; Pi recibe una skill CLI.

Estos consumidores viven del **lado del flujo de trabajo**, fuera de este repo;
llaman a la API de parser de solo lectura de `ai-r` (`read_messages`, las
herramientas MCP, los verbos). `ai-r` mismo sigue siendo un lector.

## Inicio rápido (1 solicitud)

Requisitos previos: Python 3.11+ con `venv` (`python3-venv`) o `pip`
(`python3-pip`/`pip3`), y `jq` (usado para registrar automáticamente las
configuraciones MCP de Claude y Antigravity — los demás no necesitan `jq`).

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

Eso es todo. El instalador:
- Usa modo por usuario por defecto; el modo `opt` es explícito.
- Crea un venv, instala el paquete runtime.
- Parchea las configuraciones MCP para **Claude**, **Codex**, **OpenCode**,
  **Antigravity** cuando esos archivos de configuración existen.
- Instala la skill CLI de **Pi** en `~/.agents/skills/ai-r/SKILL.md` si no existe.
- Ejecuta pruebas de humo.

## Agentes soportados

| Agente | Almacenamiento | Parser |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite (detecta snap/flatpak) |
| Antigravity | `~/.gemini/antigravity/brain/` | directorios brain JSON / markdown |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

¿No está tu agente? Añadir un sexto es **un solo módulo de parser** — el patrón
de solo lectura se porta a cualquier herramienta (Cursor, Cline, la tuya) en
minutos. Ver [CONTRIBUTING.md](./CONTRIBUTING.md).

## Superficies

`ai-r` expone el mismo poder de lectura de tres maneras:

- **Servidor MCP** (`ai-r-mcp`) — 13 herramientas sobre stdio JSON-RPC, para que
  cualquier agente con capacidad MCP pueda llamarlo directamente (recomendado).
- **CLI** (`ai-r`) — subcomandos para scripts y uso manual.
- **SDK de Python** (`from ai_r.parsers import ...`) — los parsers, los modelos
  tipados `Session`/de mensajes y los verbos de eventos, para construir tus
  propias herramientas.

### Vocabulario de métodos (SSOT)

El bloque de abajo está enmarcado desde [`docs/methods.md`](./docs/methods.md) —
la única fuente de verdad para los verbos y presets públicos. Se mantiene
sincronizado con el bloque marcador de ese archivo.

<!-- methods:start -->

## Verbs

| verb | purpose | parameters |
|---|---|---|
| `query` | filter/search session events; `with_intent=True` → a top-level `intent` on each event (the same `previous_user_intent` as legacy) | type, agent, session, since, until, file, tool, text, sort(relevance\|date), relative_to+direction(prev\|next)+n(1\|all), step_type, limit, with_intent; kind/parent/group — stubs (Phase 3) |
| `plan` | normalized plan atoms of a session (final vs drafts, grouped by task) | session, kind(draft\|final\|completed_major), group=task, agent |
| `get_body` | on-demand body by event/plan id | id, shallow |
| `aggregate` | rollup over rows (query/find_file_edits/session-inventory) → `{groups, totals}`; `rank_by=stats` gives the session_stats order (sessions→edits→label), `kind_split=True` adds `kind_split_available`/`note` | rows, group_by(field\|callable), metrics ⊆ count\|sessions\|edits\|intents\|agents\|messages\|files, rank_by(default\|stats), kind_split |
| `diff` | stitch edit-rows into a per-file unified diff (bodies on-demand via message_index; `intent` taken from the row when `query(with_intent)`) → `{files:[{file,edits,diff,hunks}], count, caveats}` | rows, per_file=True, format=unified |
| `detect_current` | runtime identity (env/fs, outside session-query) → `{session_id, agent, candidates[], verified, self}` | agent (hint) |

## Presets

| preset | expansion |
|---|---|
| `intent(event, n)` | `query(relative_to=event, direction=prev, n)` |
| `reaction(event, n)` | `query(relative_to=event, direction=next, n)` |
| `plan(session, kind, group=task)` | `query(type=plan_event, …)` → normalized + kind-tagged (final/draft/completed_major) |
| `session_stats(group_by)` | builds per-session inventory rows → `aggregate(rows, group_by, rank_by=stats, kind_split=True)` → projection to the legacy totals shape |
| `session_diff(uuid, agent≠codex)` | `diff(query(type=edit\|write, session=uuid, with_intent=True) with file-ref)` → projection (no file-level `hunks`) |

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
| `find_file_edits` / `find_tool_calls` | the record carries `session_title`/`session_date`/`assistant`/`input`, which are NOT in a `query` event; reproducing them = re-reading the session (not a *thin* preset but a second parse over events — strictly slower) + loss of codex shell-redirect edits. `intent` is now reproducible (`with_intent`), but the other fields are not. SSOT of the rich edit/tool record |
| `search_sessions` | session-granular + BM25 session snippets; `query` is event-granular (turn/tool) → no clean 1:1 |
| `detect-agent`/`detect-session` (CLI) | the CLI prints the agent `source` and 6 output modes (list/first/strict/self/fingerprint/`--json`/`--count`) + a WARN line; the `detect_current` dict does not provide this |

## Plan atom (normalized, agent differences hidden)

`Plan { id, session_id, agent, title, task_id, kind: draft\|final\|completed_major, path?, steps?, status?, refs[], sha256 }`. Body/steps — on-demand via `get_body(id, shallow?)`. `shallow=True` → only the task's final, draft bodies dropped (scenario S6).

**Grouping by task = `task_id` (stable key):** for Claude it's the plan slug `plans/<slug>.md` (Write carries the path directly; `ExitPlanMode` without a path inherits the slug of the nearest preceding plan-Write in the session; if there is no slug yet — fallback to the normalized title). For Antigravity — the `implementation_plan.md` path. For Codex (no file) — the normalized title (a continuous `update_plan` run). Keyed by slug, NOT by title, because the title drifts within one iteration chain (decorations change the heading) — on real data that split one task into several. In a group the last plan_event by (ts, seq) = `final`, the earlier ones = `draft`; strictly earlier tasks (a DIFFERENT slug) = `completed_major`. The internal parser→signal table (`ExitPlanMode`/`Write plans/*.md`/`update_plan`/`implementation_plan.md`) is an implementation detail, invisible from outside.

<!-- methods:end -->

### Lo que aporta esta rama — el núcleo de eventos

Los verbos de arriba son nuevos: un **núcleo de flujo de eventos** reemplaza un
montón de herramientas puntuales. Puntos destacados:

- **`query`** — el caballo de batalla. Filtra el flujo de eventos unificado por
  `type` / `agent` / `session` / fecha / `file` / `tool` / `text`. Con
  `sort="relevance"` la coincidencia de texto se rankea con BM25 (el mismo
  scorer que `search_sessions`). Con `relative_to`+`direction`+`n` recorre los
  turnos vecinos — el primitivo detrás tanto de `intent` como de `reaction`.
- **presets `intent` / `reaction`** — `intent(event)` = la petición del usuario
  *detrás* de un evento (retroceder); `reaction(event)` = la respuesta del
  usuario *después* de un turno del asistente (avanzar — crítica, corrección,
  aprobación).
- **`plan`** — átomos de plan normalizados por sesión, agrupados por tarea,
  etiquetados `final` vs `draft` vs `completed_major`. Así puedes extraer *el
  plan en el que el agente se asentó* frente a las revisiones descartadas — a lo
  largo de Claude, Codex y Antigravity, cuyas señales de plan difieren.
  `get_body(..., shallow=True)` entrega a un subagente solo el plan final, con
  los borradores elididos.
- **`aggregate` / `diff` / `detect_current`** — rollup genérico, diff cosido por
  archivo e identidad propia en runtime. `session_stats` y `session_diff` son
  ahora presets ligeros sobre estos, con salida byte a byte idéntica probada en
  datos reales (ver el bloque SSOT de arriba).

Alcance honesto: esto es **extracción de entidades de solo lectura** — turnos,
llamadas a herramientas, planes, intents, reacciones. **No** es un grafo ni un
almacén de memoria. Lo que un consumidor hace después (dividir en un grafo de
conocimiento, Obsidian, memoria persistente) queda deliberadamente **fuera de
alcance** y vive del lado del consumidor.

### Herramientas MCP

El servidor MCP expone 13 herramientas. Los elementos esenciales de lectura:

| Herramienta | Propósito |
|---|---|
| `list_sessions(agent?, limit?, offset?)` | Lista sesiones descubribles, opcionalmente filtradas por agente. Paginada. |
| `read_session(uuid, agent, offset?, limit?)` | Lee una sesión; hasta 100 mensajes por defecto, `offset`/`limit` para paginar. |
| `find_file_edits(path, agent?, since?, until?, limit?)` | Cada edición de archivo de una ruta, entre agentes por defecto, opcionalmente por periodo. |
| `find_tool_calls(tool_name?, tool_name_pattern?, agent?, since?, until?, limit?)` | Cada llamada a herramienta — shell, escrituras de archivos, peticiones web, llamadas MCP — cada una llevando la petición del usuario que la disparó como `intent`. |
| `search_sessions(query, agent?, scope?, operator?, limit?, sort?)` | Busca por título y/o cuerpo con `AND`/`OR`/`NOT` y `-term` estilo Google; `sort=relevance` (BM25) o `date`. |
| `session_stats(agent?, since?, until?, group_by?, top?)` | Agrupa + rankea sesiones por `agent`/`dir`/`date`/`kind`. |
| `session_diff(session_uuid, agent, path?)` | Reconstruye lo que cambió una sesión, por archivo, sin git. |
| `query`, `plan`, `get_body`, `aggregate`, `diff`, `detect_current` | Los verbos del núcleo de eventos descritos arriba. |

**Paginación** (`limit`/`offset`, más un flag `truncated` cuando quedan más
páginas) está expuesta en las herramientas MCP y en el SDK de Python — ver
[architecture.md](docs/architecture.md).

### CLI

```bash
# list / read / search
ai-r list --agent pi
ai-r read --agent pi <session-uuid>
ai-r search "refactor"
ai-r search "pwa manifest" --scope body --operator and --agent claude

# who edited a file, across all agents, optionally time-boxed
ai-r find-file-edits src/auth.py --since 2026-06-01 --until 2026-06-30
ai-r find-file-edits "config" --agent claude --limit 20

# what did agents run? exact tool name or substring pattern, time-boxed
ai-r find-tool-calls Bash --since 2026-06-01
ai-r find-tool-calls --pattern deploy --agent codex

# which files change most? rank by edits / sessions / distinct requests / agents
ai-r file-frequency --top 10
ai-r file-frequency --path src/ --agent claude --since 2026-06-01

# which agent / session am I in (scripts, orchestration, self-resume)
ai-r detect-agent --quiet          # → e.g. "claude"
ai-r detect-session --json         # → candidate session UUIDs

# render a session as a CHANGELOG round (handoff doc / replay)
ai-r export rounds <session-uuid> --include-round --output round.md
```

Añade `--json` a la mayoría de subcomandos para salida legible por máquina. Los
verbos del núcleo de eventos (`query`/`plan`/`aggregate`/`diff`/`detect_current`)
están disponibles vía MCP y el SDK de Python; el CLI cubre los subcomandos
listados arriba.

#### Operadores de búsqueda

`search_sessions` (MCP) y `ai-r search` (CLI) comparten el mismo parser de
consulta y el mismo parámetro operator. El comportamiento por defecto
(`scope="title"`, `operator="AND"`, `limit=50`) es la búsqueda histórica por
subcadena solo en título.

**Sintaxis de consulta**

| Forma | Ejemplo | Significado |
|---|---|---|
| Palabras sueltas | `pwa manifest` | Ambos términos (operator controla cómo). |
| Frase entre comillas | `"exact phrase"` | Un único término literal. |
| Prefijo negativo | `-claude` | Estilo Google: este término NO debe aparecer. |

Las palabras `AND`, `OR` y `NOT` dentro de la consulta son términos de búsqueda
literales. El comportamiento booleano se selecciona con `--operator and|or|not`
(CLI) o `operator="AND"|"OR"|"NOT"` (MCP).

**Modos operator** (controla cómo se combinan los términos positivos)

| Modo | Semántica de `pwa manifest` | Semántica de `pwa -claude` |
|---|---|---|
| `AND` (por defecto) | ambos deben aparecer | `pwa` aparece, `claude` no |
| `OR` | al menos uno aparece | alguno de `pwa` aparece, `claude` no |
| `NOT` | ninguno aparece | ni `pwa` ni `claude` aparecen |

**Modos scope**

| Scope | Dónde se ejecuta la búsqueda |
|---|---|
| `title` (por defecto) | solo `session.title` — coincide con el comportamiento histórico de solo título. |
| `body` | texto de mensajes + `tool_use[*].input` + `tool_result[*].content` de cada sesión. |
| `all` | título o cuerpo. |

Cuando `scope` es `body` o `all` y hay coincidencia, el resultado incluye un
campo `snippet` (CLI: se imprime en la tabla) — el primer extracto coincidente,
hasta 200 caracteres. Los resultados se rankean con BM25 por defecto
(`sort=relevance`); pasa `sort=date` para ordenar por recencia.

**Nota de rendimiento**: `body` y `all` invocan `read_messages` en cada sesión
candidata. En almacenes grandes la primera corrida puede ser lenta; sube
`--limit` para mantener el conjunto de resultados acotado al iterar.

**Ejemplo MCP**

```python
search_sessions(
    query='pwa -claude',
    agent='claude',
    scope='body',
    operator='AND',
    limit=20,
)
```

**Ejemplos CLI**

```bash
# title-only (legacy, still default)
ai-r search "refactor"

# body search, all terms must appear, exclude claude
ai-r search "pwa manifest -claude" --scope body --operator and

# body search, any term, max 5 results
ai-r search "pwa manifest" --scope body --operator or --limit 5

# everything containing neither of these terms
ai-r search "auth login" --scope body --operator not
```

### SDK de Python

```python
from ai_r.parsers import AgentName, claude

for session in claude.list_sessions():
    print(session.uuid, session.title)

session = claude.read_session("<session-uuid>")
print(session.message_count)

messages = claude.read_messages("<session-uuid>")
print(messages[0].role, messages[0].text)
```

Ver [docs/architecture.md](./docs/architecture.md) para la estratificación completa.

## Casos de uso — un trabajo por consumidor real

Un único lector sobre todos los agentes desbloquea trabajos que el registro de
un solo agente no puede hacer:

- **Atribución entre agentes — «¿qué agente hizo esto?»** Cada edición a una
  ruta, cada llamada a herramienta, en todos los agentes y sesiones, etiquetada
  con la petición que la disparó. Acótalo por periodo: «¿qué le hicieron los
  agentes a `src/auth.py` la semana pasada?» — `find-file-edits` /
  `find-tool-calls`. Impulsa el **git-log-auditor**.
- **Auditoría y replay — revisar fríamente lo que un agente realmente hizo.** Un
  agente nuevo lee una sesión previa y reporta lo que *ejecutó*, no solo lo que
  afirmó. `session_diff` reconstruye el cambio por archivo sin git;
  `export rounds` renderiza un traspaso estilo CHANGELOG. Impulsa
  **session-summarizer** y **ai-local-reader**.
- **Reanudación y traspaso — cambia de agente a mitad de tarea, mantén el
  hilo.** ¿Alcanzaste un límite del proveedor o se te agotó la ventana de
  contexto? Empieza una sesión nueva (cualquier agente), pásale el UUID de la
  sesión anterior y continúa. La transcripción previa es legible sin importar
  qué herramienta la escribió — `read_session`, `detect-session`.
- **Encuentra ediciones de archivos + intents — ¿por qué este archivo no dejó
  de cambiar?** `file-frequency` agrega qué archivos rotan más, rankeados por
  ediciones, sesiones distintas, peticiones distintas y agentes involucrados;
  cada edición lleva la petición del usuario detrás como `intent`.
- **Extracción de planes — recupera el plan en el que el agente se asentó.**
  `plan` devuelve átomos de plan normalizados por tarea, `final` frente a
  `draft`, a lo largo de Claude / Codex / Antigravity. Entrega a un subagente
  solo el plan final con `get_body(..., shallow=True)`.

## Diferenciadores frente a alternativas

*Validado vía WebSearch, 2026-07-01.* El espacio de visores de un solo agente
está saturado (claude-code-viewer, claude-code-history-viewer,
claude-session-viewer, simonw/claude-code-transcripts, claude-view); un puñado
de herramientas más nuevas *sí* son entre agentes (jazzyalex/agent-sessions,
Dicklesworthstone/coding_agent_session_search, hacktivist123/
agent-session-resume). En qué difiere `ai-r`:

| Capacidad | Visores de un solo agente | Herramientas de sesión entre agentes | `ai-r` |
|---|---|---|---|
| Lee registros de >1 agente | No | Sí | Sí — Claude, Codex, OpenCode, Antigravity, Pi |
| Superficie programática | Mayormente GUI/TUI | Mayormente TUI/CLI/app | **MCP + CLI + SDK de Python** |
| Atribución (edición/comando → agente + intent) | — | Parcial (procedencia en algunas) | Sí — `find-file-edits` / `find-tool-calls`, cada una con `intent` |
| Audit-replay (reconstruir lo que cambió una sesión, sin git) | — | Raro | Sí — `session_diff` |
| Extracción de planes (final vs draft, normalizado) | — | — | Sí — `plan` |
| Alcance | Visor | Búsqueda / reanudación / memoria | **Núcleo de extracción de solo lectura** (grafo/memoria queda a los consumidores) |

Algunas herramientas entre agentes van en la dirección *contraria* — hacia
memoria persistente o capas de coordinación (p. ej. `cass_memory_system`,
`mcp_agent_mail`). `ai-r` se detiene deliberadamente en la extracción de solo
lectura: la memoria y los grafos son del lado del consumidor, no vienen
integrados. Donde las capacidades exactas de un competidor no queden claras en
la documentación pública, la tabla de arriba se queda corta en vez de exagerar.

## Límites de diseño — un lector, no un guardia

- **Solo lectura.** `ai-r` nunca ejecuta código de agente y nunca escribe en el
  almacenamiento de sesiones del agente. Lee y devuelve.
- **Sin grafo, sin memoria.** Extrae entidades (turnos, llamadas a herramientas,
  planes, intents). Construir un grafo de conocimiento o memoria persistente
  encima es trabajo de un consumidor, fuera del alcance de este repo.
- **No es una capa de control de acceso.** Cualquier llamador que pueda alcanzar
  el CLI, el servidor MCP o el paquete puede leer cualquier sesión — no hay
  autorización frente a los parsers. Mantenlo donde llamadores locales no
  confiables no puedan alcanzarlo.
- **El contenido de las sesiones no es confiable.** El llamador de un lector
  (auditor, resumidor, agente de replay) debe tratar el contenido de sesión como
  *datos, no instrucciones*. Ver
  [Seguridad — contenido de sesión no confiable](docs/security.md).

Los revisores, resúmenes y auditorías específicos de cada flujo viven fuera de
este repo y consumen la API del parser (`read_messages`).

### Limitaciones conocidas

- **Antigravity** — cobertura con fixtures más pruebas de humo opcionales con datos reales cuando existe un directorio brain local.
- **Ediciones shell del Codex CLI** — `find_file_edits` recupera escrituras de archivos de codex desde comandos shell `exec_command` / `local_shell_call` mediante un escaneo conservador de redirecciones con conocimiento de comillas (`>` / `>>`). Las escrituras hechas con `tee` / `sed -i` / `cp` / `mv` / solo heredoc no se detectan; las ediciones estructuradas (`apply_patch` / `write_file`) siempre se detectan.

Ver [docs/parsers.md](docs/parsers.md) para la matriz completa de cobertura de parsers.

## Registro MCP

`ai-r-mcp` es un servidor MCP stdio. Regístralo una vez por herramienta
anfitriona. Sustituye `USER` por tu nombre de usuario (o quita la ruta absoluta
si `ai-r-mcp` está en tu `PATH`). **Reinicia la herramienta anfitriona tras
editar su configuración** — ninguna recoge cambios MCP en vivo.

Los fragmentos de abajo usan `/home/USER/.local/bin/ai-r-mcp`. Ajusta la ruta si
tu instalación está en otro lugar (`which ai-r-mcp` te lo dice).

### Claude Code

Edita `~/.claude.json` (objeto `mcpServers` de nivel superior):

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

Para registro de un solo proyecto, haz commit de un `.mcp.json` en la raíz del
repo (ver [`.mcp.json`](./.mcp.json)).

### Codex

Edita `~/.codex/config.toml`:

```toml
[mcp_servers.ai-r]
command = "/home/USER/.local/bin/ai-r-mcp"
args = []
```

### Gemini CLI

Edita `~/.gemini/settings.json` (objeto `mcpServers`):

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

Edita `~/.config/opencode/opencode.jsonc` (objeto `mcp` de nivel superior).
OpenCode se diferencia de los demás en tres cosas: `type` es `"local"` (no
`"stdio"`), `command` es un único array fusionado (comando + args juntos), y la
clave de entorno es `"environment"`.

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

Edita `~/.gemini/antigravity/mcp_config.json` (objeto `mcpServers`). Es distinto
de la configuración de Gemini CLI de arriba — Antigravity guarda su configuración
MCP bajo `~/.gemini/antigravity/`.

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

### Pi — skill, no MCP

Pi (`@earendil-works/pi-coding-agent`) **no tiene configuración de servidor MCP**
que editar. Usa un modelo de extensiones/skills (`pi install <source>`,
`pi config`), no un mapa `mcpServers`, así que `ai-r-mcp` no puede registrarse
como herramienta MCP in-process dentro de Pi (y lanzarlo in-process violaría el
contrato de diseño de Pi). En su lugar, `install/agent-configs.sh` coloca una
**skill CLI** de solo lectura en `~/.agents/skills/ai-r/` — un directorio que Pi
ya escanea. La skill enseña al modelo a llamar al CLI `ai-r` desde una sesión
bash de Pi, sin lanzar MCP. Las sesiones de Pi también son completamente legibles
*por* `ai-r` vía CLI (`ai-r list --agent pi`, `ai-r read …`) o el SDK de Python;
ambos leen los archivos `~/.pi/agent/sessions/` directamente. Para un comando
slash `/ai-r`, pon `enableSkillCommands: true` en `~/.pi/agent/settings.json` (el
texto de la skill funciona incluso con el `false` por defecto).

### Notas

- `ai-r-mcp` debe estar en `PATH`, o usa la ruta absoluta como arriba.
- El parcheo de configuración JSON usa `jq`. Si falta `jq`, los registros de
  Codex, OpenCode y Pi igual se completan; las configuraciones de Claude y
  Antigravity se omiten — instala `jq` o regístralas a mano con los fragmentos
  de arriba.
- Reinicia la herramienta anfitriona tras editar su archivo de configuración.
- El servidor es de solo lectura; cualquier llamador que lo alcance puede leer
  cualquier sesión. Ver [Límites de diseño](#límites-de-diseño--un-lector-no-un-guardia).

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

## Desarrollo

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ pruebas, cobertura ≥80% requerida por CI.
- Conventional Commits (`feat:`, `fix:`, `docs:`, …).
- Ver [CONTRIBUTING.md](./CONTRIBUTING.md) y [docs/parsers.md](./docs/parsers.md) para añadir nuevos agentes.
- `src/ai_r/validators/` y `src/ai_r/templates/` son helpers standalone
  opcionales (validación de markdown session-note), no son parte de la
  superficie del CLI o MCP.

<details>
<summary>Keywords</summary>

claude code session reader · claude code session parser · codex session parser ·
opencode session reader · antigravity brain parser · pi agent session reader ·
cross-agent attribution · ai coding agent audit · ai agent session history ·
mcp session tools · read-only session reader · agent session replay ·
resume agent session · agent handoff · plan extraction · tool-call audit ·
file edit attribution · multi-agent coding · claude codex opencode antigravity pi

</details>

## Licencia

MIT — ver [LICENSE](./LICENSE).

---

**Empieza ya:** clona + `bash install.sh`, luego registra el servidor MCP para
tu agente ([Claude](#claude-code) · [Codex](#codex) ·
[OpenCode](#opencode) · [Antigravity](#antigravity) · [Pi](#pi--skill-no-mcp))
y reinicia la herramienta anfitriona. Una única superficie de solo lectura para
el historial de todos los agentes.
