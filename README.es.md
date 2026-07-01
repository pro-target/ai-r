# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> `git` muestra **qué** llegó al código. `ai-r` muestra **por qué**: qué agente
> lo hizo, bajo qué plan — y si silenciosamente descartó el plan que realmente
> había acordado. Solo lectura, a través de los cinco agentes de programación, una
> sola interfaz.

Un agente informa: "hecho X, según el plan Y". No tienes forma de comprobarlo. El
plan vive en un formato, las ediciones en otro. Y si dos agentes trabajaron la
tarea, sus historiales no se reconcilian en absoluto — cada uno escribe a su
manera, en su propio lugar. `ai-r` lee el historial de sesión de un agente y
extrae la intención, el plan y la autoría detrás de una edición.

## Ejemplo rápido — un agente pregunta sobre el historial

El modo principal es **MCP**: un agente (Claude, Codex, …) llama a `ai-r`
directamente y pregunta sobre el historial en lenguaje natural. Por ejemplo —
recuperar el plan que el agente anterior acordó, descartando los borradores:

```
Show me the plan from the last session — final only, no intermediate revisions.
→ ai-r: plan(session=…, kind="final")  →  get_body(id, shallow=true)
        returns the final task + a list of dropped_drafts
```

Atribución rápida de ediciones — un solo comando de terminal, en todos los
agentes a la vez:

```bash
# who edited this file, and when — cross-agent, optionally time-boxed
ai-r find-file-edits auth.py --since 2026-06-01
```

## Qué duele

- "Hecho, hice X según el plan Y" — sin nada contra lo que comprobarlo: el agente
  mantiene el plan en una forma, las ediciones en otra.
- Cambiaste de agente a mitad de la tarea y perdiste el hilo. No hay dónde
  preguntar "¿qué había intentado ya el *otro* agente?".
- Una edición aparece en un archivo — y no está claro **qué** agente la hizo, ni
  bajo qué petición.

Una sola causa: cada agente escribe su historial **a su manera** — Claude y Codex
en JSONL, OpenCode en SQLite, Antigravity en directorios "brain", Pi en JSONL por
proyecto. Cinco formatos, cinco disposiciones — juntos no se reconcilian.

## La promesa

`ai-r` fusiona los cinco en **una sola interfaz de solo lectura**. Apunta
cualquier agente — o un script, o tú mismo — a cualquier sesión, sin importar qué
herramienta la registró. Una sola forma de consulta por agente; las diferencias
de formato se normalizan dentro de los parsers.

## Características clave

- **"¿Por qué?", no solo "¿Qué?".** Extrae el plan, la intención y la autoría
  detrás de una edición — no solo el texto del diff. `git diff` te dice *qué*
  cambió; `ai-r` te dice bajo qué plan y a petición de quién.
- **El plan final, no los borradores.** `ai-r` recupera el plan que el agente
  *acordó*, y por separado muestra lo que descartó por el camino
  (`dropped_drafts`) — a través de Claude / Codex / Antigravity, donde las señales
  del plan difieren.
- **Atribución multiagente.** Cualquier edición de archivo o llamada de
  herramienta → el agente que la hizo, más la petición que la disparó
  (`find-file-edits` / `find-tool-calls`).
- **Respuesta pequeña, cuerpo bajo demanda.** Los registros llevan una referencia
  al contenido (hash + longitud); el texto completo de la edición se obtiene por
  separado — la respuesta no se dispara.
- **Funciona sobre MCP (13 herramientas).** Un agente llama a `ai-r` directamente
  en lenguaje natural; los mismos datos están disponibles desde la terminal (CLI)
  y desde código (SDK de Python).
- **Un lector, no un guardián.** Extrae entidades; tú (o tu herramienta)
  construyes el grafo de conocimiento y la memoria. Solo lectura: nunca ejecuta ni
  escribe en el historial de un agente.

## Para qué lo usas

- **Auditar sesiones con una mirada fresca.** Un agente nuevo con un contexto
  vacío revisa fríamente sesiones pasadas en tres ejes: ¿se cumplieron promesas y
  requisitos; son sólidas y bien juzgadas las decisiones; con qué profundidad se
  exploró la cuestión — qué se le pasó al agente? En una ejecución real, se
  revisaron 271 diálogos de esta forma en una semana, pillando a agentes que
  terminaron la tarea **pero engañaron sobre la planificación** — algo que un chat
  en vivo oculta, y que te lleva a decisiones equivocadas.
- **Continuar más allá de un contexto agotado — sin perder detalle.** `/compact`
  borra los detalles. En su lugar, abre una sesión nueva: lee los **registros** de
  la sesión anterior y continúa desde sus conclusiones, sin volver a quemar
  contexto en lo que ya se resolvió. La sesión original queda intacta — para
  auditoría y búsqueda. La nueva sesión puede correr en **cualquier** agente: el
  historial se reconcilia sin importar la herramienta.
- **Alimenta tu sistema de memoria.** ¿Mantienes memoria y resúmenes al estilo
  Karpathy, o tu propio método? `ai-r` te da, para los chats de IA, lo que ya
  haces con el historial de mensajes — entidades parseadas para construir una
  memoria duradera de los detalles que importan.
- **Recordar qué hiciste y por qué.** ¿Por qué se editó este archivo? ¿Por qué se
  añadió esta regla? Encuentra la sesión donde el archivo cambió y lee la petición
  *anterior* a la edición.

## En qué se diferencia de las herramientas de búsqueda de sesiones

Un puñado de herramientas multiagente ahora leen el historial de más de un agente
(`jazzyalex/agent-sessions`, `Dicklesworthstone/coding_agent_session_search`,
`hacktivist123/agent-session-resume`). Casi todas van sobre **búsqueda y línea de
tiempo**: encontrar una *sesión*, recorrer el historial.

`ai-r` va más profundo: extrae el **plan, la intención y la autoría como
entidades listas** sobre las que construyes memoria. La búsqueda encuentra texto —
`ai-r` responde **por qué**. Técnicamente una herramienta de búsqueda también
podría desenterrar un plan del texto de una sesión, pero no lo devuelve parseado
en una forma única y normalizada — con `ai-r` esa es la superficie principal.

| Capacidad | Visores de un solo agente | Herramientas de búsqueda multiagente | `ai-r` |
|---|---|---|---|
| Lee logs de >1 agente | No | Sí | Sí — Claude, Codex, OpenCode, Antigravity, Pi |
| Superficie programática | Mayormente GUI/TUI | Mayormente TUI/CLI/app | **MCP + CLI + SDK de Python** |
| Atribución (edición/comando → agente + intención) | — | Parcial | Sí — `find-file-edits` / `find-tool-calls` |
| Replay de auditoría (reconstruir los cambios de una sesión, sin git) | — | Raramente | Sí — `session_diff` |
| Extracción de plan (final vs borrador, normalizado) | — | — | Sí — `plan` |
| Alcance | Visor | Búsqueda / reanudar / memoria | **Núcleo de extracción de solo lectura** |

*Las columnas de los competidores reflejan su documentación pública a fecha de 2026-07; donde una capacidad no está clara, subestimamos en lugar de sobreafirmar.*

Deliberadamente **no** competimos en amplitud de agentes, velocidad ni riqueza de
TUI. La cuña de `ai-r` es extraer el "por qué" y entidades estructuradas para
consumo por máquina.

## Probado en la práctica

`ai-r` ya lee su propio historial de desarrollo — a través de los cinco agentes.
Herramientas reales corren sobre él (viven por separado, sobre su API de solo
lectura):

- **auditor** — un agente fresco revisa fríamente qué hizo y decidió realmente el
  anterior. Esto pilló a agentes que mintieron discretamente sobre el plan.
- **summarizer** (`export rounds`) — renderiza una sesión en un documento de
  traspaso listo para usar.
- **ai-local-reader** — un skill de solo lectura: audita sesiones pasadas desde
  disco a través de todos los agentes.

Estas herramientas están del lado del flujo de trabajo, fuera de este repo. `ai-r`
en sí solo lee y devuelve datos.

## Agentes soportados

| Agente | Almacenamiento | Parser |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite (autodetección snap/flatpak) |
| Antigravity | `~/.gemini/antigravity/brain/` | Directorios brain JSON / markdown |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

¿No es tu agente? Añadir un sexto es **un solo módulo parser**; el patrón de solo
lectura se porta a cualquier herramienta en minutos. Ver
[CONTRIBUTING.md](./CONTRIBUTING.md).

## Superficies

`ai-r` ofrece el mismo poder de lectura de tres formas:

- **Servidor MCP** (`ai-r-mcp`) — 13 herramientas sobre JSON-RPC por stdio, para
  que cualquier agente MCP lo llame directamente (recomendado). Registro — ver
  [docs/mcp-registration.md](./docs/mcp-registration.md).
- **CLI** (`ai-r`) — subcomandos para scripts y uso manual (`list` / `read` /
  `search` / `find-file-edits` / `find-tool-calls` / `file-frequency` /
  `detect-agent` / `export rounds`). Operadores de búsqueda —
  [docs/search-operators.md](./docs/search-operators.md).
- **SDK de Python** (`from ai_r.parsers import ...`) — parsers, modelos tipados
  `Session`/mensaje, y los verbos de eventos, para construir tus propias
  herramientas.

### Vocabulario de métodos (SSOT)

El bloque de abajo se enmarca desde [`docs/methods.md`](./docs/methods.md) — la
fuente de verdad en inglés para los verbos y presets públicos. Se mantiene
sincronizado con el bloque de marcadores de ese archivo.

<!-- methods:start -->

## Verbs

| verb | purpose | parameters |
|---|---|---|
| `query` | filter/search session events; `with_intent=True` → a top-level `intent` on each event (the same `previous_user_intent` as legacy); a `tool_call` event carries an `is_error` outcome ref when its result is correlatable (see *Output bounds & outcome* below) | type, agent, session, since, until, file, tool, text, sort(relevance\|date), relative_to+direction(prev\|next)+n(1\|all), step_type, limit, with_intent; kind/parent/group — stubs (Phase 3) |
| `plan` | normalized plan atoms of a session (final vs drafts, grouped by task) | session, kind(draft\|final\|completed_major), group=task, agent |
| `get_body` | on-demand body by event/plan id; returned body/text is bounded by `max_chars` (default 500k) → over-long bodies are cut with a marker and flagged `body_truncated` | id, shallow, max_chars |
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
| `find_file_edits` / `find_tool_calls` | the record carries `session_title`/`session_date`/`assistant`/`input`, which are NOT in a `query` event; `find_tool_calls` additionally carries per-record `is_error` (correlated tool-call outcome) and `output` (correlated tool-result content, char-capped); reproducing them = re-reading the session (not a *thin* preset but a second parse over events — strictly slower) + loss of codex shell-redirect edits. `intent` is now reproducible (`with_intent`), but the other fields are not. SSOT of the rich edit/tool record |
| `search_sessions` | session-granular + BM25 session snippets; `query` is event-granular (turn/tool) → no clean 1:1 |
| `detect-agent`/`detect-session` (CLI) | the CLI prints the agent `source` and 6 output modes (list/first/strict/self/fingerprint/`--json`/`--count`) + a WARN line; the `detect_current` dict does not provide this |

## Plan atom (normalized, agent differences hidden)

`Plan { id, session_id, agent, title, task_id, kind: draft\|final\|completed_major, path?, steps?, status?, refs[], sha256 }`. Body/steps — on-demand via `get_body(id, shallow?)`. `shallow=True` → only the task's final, draft bodies dropped (scenario S6).

**Grouping by task = `task_id` (stable key):** for Claude it's the plan slug `plans/<slug>.md` (Write carries the path directly; `ExitPlanMode` without a path inherits the slug of the nearest preceding plan-Write in the session; if there is no slug yet — fallback to the normalized title). For Antigravity — the `implementation_plan.md` path. For Codex (no file) — the normalized title (a continuous `update_plan` run). Keyed by slug, NOT by title, because the title drifts within one iteration chain (decorations change the heading) — on real data that split one task into several. In a group the last plan_event by (ts, seq) = `final`, the earlier ones = `draft`; strictly earlier tasks (a DIFFERENT slug) = `completed_major`. The internal parser→signal table (`ExitPlanMode`/`Write plans/*.md`/`update_plan`/`implementation_plan.md`) is an implementation detail, invisible from outside.

## Output bounds & tool-call outcome

**Bounded output (untrusted sessions can be huge — the surface never returns unbounded bytes):** `find_tool_calls` caps each record's `input`/`assistant`/`intent`/`output` fields (over-long values cut with a `…[truncated]` marker and named in a per-record `truncated_fields`) and stops appending once a total-response byte budget is hit, flagging `output_truncated`; this is distinct from the count-based `truncated` (more records exist). `get_body` bounds the body via `max_chars` (`body_truncated`). Tool input larger than 1 MB is never JSON-decoded (returned verbatim) — a shared guard on the event stream and `find_tool_calls` alike. `read_session` renders a tool result as `[tool_result ok: <snippet>]` or `[tool_result ERROR: <snippet>]` (was a bare `[tool_result]`).

**`is_error` (tool-call outcome) is cross-agent best-effort:** **Claude** and **OpenCode** carry a real success/error flag (Claude's `tool_result.is_error`; OpenCode's `state.status == "error"`). **Codex** and **Pi** expose no error field on their result records → `is_error` is always `False` (absence of a flag, not a proof of success). **Antigravity** emits no tool-result records at all → no outcome signal. Consumers must not read a cross-agent `is_error=False` as "verified success" for Codex/Pi/Antigravity. `find_tool_calls` now carries the same `is_error` per record, plus the correlated `output` (tool-result content, char-capped) — correlation is by tool_use_id (Claude `tool_use.id` / OpenCode `callID`); with the same best-effort caveat (`is_error` is authoritative only for Claude/OpenCode, and defaults to `False` for Codex/Pi/Antigravity or when no result correlates).

<!-- methods:end -->

### Núcleo de eventos

Los verbos de arriba son nuevos: un solo **núcleo de eventos** reemplaza un montón
de herramientas puntuales. Cada parser lee los logs de un agente y emite modelos
tipados, normalizados en un único flujo neutral respecto al agente — `user_turn`
/ `assistant_turn` / `tool_call(...)` / `plan_event`. Un pequeño conjunto de
verbos filtra, agrega y compara ese flujo; las diferencias de agente
(`ExitPlanMode` vs `update_plan` vs `implementation_plan.md`) quedan ocultas
dentro de los parsers — quien llama ve una sola forma.

Un límite honesto: esto es **extracción de entidades únicamente** — turnos,
llamadas de herramienta, planes, intenciones, reacciones. **No** es un grafo y
**no** es un almacén de memoria. Lo que hagas después (grafo de conocimiento,
Obsidian, memoria persistente) queda de tu lado, fuera de este repo. Para la
estratificación completa y la lista de herramientas MCP, ver
[docs/architecture.md](./docs/architecture.md).

## Inicio rápido (1 comando)

Requisitos: Python 3.11+ con `venv` o `pip`, y `jq` (usado para auto-parchear las
configuraciones MCP de Claude y Antigravity — las demás no necesitan `jq`).

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

El instalador crea un venv, instala el paquete de runtime, parchea las
configuraciones MCP para **Claude**, **Codex**, **OpenCode**, **Antigravity**
(donde existen las configuraciones), instala el skill de CLI de **Pi**, y ejecuta
smoke tests.

## Límites: un lector, no un guardián

- **Solo lectura.** Nunca ejecuta el código de un agente ni escribe en su
  historial — lee y devuelve.
- **Sin grafo, sin memoria.** Extrae entidades (turnos, llamadas, planes,
  intenciones). Construir un grafo de conocimiento o memoria a partir de ellas es
  tu trabajo, no el suyo.
- **No es una capa de control de acceso.** Cualquiera que pueda alcanzar la CLI,
  el servidor MCP o el paquete puede leer cualquier sesión. No hay autorización
  delante de los parsers; mantenlo donde procesos locales no confiables no puedan
  alcanzarlo.
- **El contenido de la sesión es datos, no comandos.** Quien lea (auditor,
  summarizer) debe tratar el texto de la sesión como datos, no como
  instrucciones. Ver [Seguridad](docs/security.md).

<!-- scenarios:start -->

## Acceptance summary

Full spec: [docs/scenarios.md](docs/scenarios.md) — 39 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

| Function | # scenarios | Headline pass criteria |
|---|---|---|
| `query` | 8 | Facet filters return correct event shape (references, no body inlined); `relative_to`+`direction` walk yields the true prev/next turn (cross-checked vs `read_session`); `text sort=relevance` is BM25-ranked; `tool_call` events carry an `is_error` outcome (cross-agent best-effort) without changing counts; unimplemented facets `kind`/`parent`/`group` **fail loud** ("not yet supported"), never a silent result. |
| `get_body` | 4 | Body fetched on-demand by id (turn text / plan text / codex steps); `shallow=true` on a draft id returns the task's **final** body + `dropped_drafts`; codex plan `steps`/`status` populated. |
| `aggregate` | 4 | `sum(count) == len(rows)`; `rank_by=stats` order is `(-sessions,-edits,label)`; `kind_split=true` adds `kind_split_available`+`note`; empty rows → empty result, no crash. |
| `diff` | 1 | Edit rows stitch into a per-file unified diff; bodies stay on-demand. |
| `detect_current` | 1 | Returns a sensible runtime identity (`session_id`/`agent`/`candidates`/`verified`). |
| `plan` | 5 | Tasks grouped by plan-file **slug**, not title (drifting titles stay ONE task, zero false `completed_major`); N draft + 1 final by `(ts,seq)`; cross-agent codex `update_plan` normalized; no false positive from a quoted `update_plan`; empty (not error) for agents with no plan signal. |
| `session_stats` (preset) | 3 | All 4 dims (agent/dir/date/kind) give sensible counts; degenerate kind split → `kind_split_available=false`+note; **byte-identical** to manual `aggregate(rank_by=stats, kind_split=true)` on a FROZEN snapshot. |
| `session_diff` (preset) | 2 | Claude session → per-file hunks in chronological order with intent attached (cross-checked vs `read_session`); codex session reconstructs targets from `printf >`/`cat > <<EOF`, with `tee`/`sed -i`/`cp`/`mv` documented as silently skipped. |
| `find_file_edits` | 3 | Default MCP call is **reference-by-default** (`input_sha256`+`input_chars`, NOT full `input`); `include_input=true` restores the body; body otherwise fetched on-demand via `get_body`. |
| `list_sessions` | 1 | Newest-first, paginated (`limit`/`offset`, `truncated` flag) inventory; each summary carries `kind`+`parent_uuid`; `agent` filter narrows the set. |
| `find_tool_calls` | 2 | Exact `tool_name` vs substring `tool_name_pattern` search, cross-agent; neither/both arguments **fail loud** (`invalid_argument`), never a silent empty result; each record surfaces the correlated `is_error` outcome + char-capped `output` (authoritative for Claude/OpenCode, best-effort elsewhere). |
| `read_session` | 2 | Reads one session into the compact `{role, content}` projection with metadata + pagination echo; `offset`/`limit` page a stable ordered list, `total` invariant across slices. |
| `search_sessions` | 3 | Title/body/all scope; `AND` default, `OR` widens (`AND ⊆ OR`), negative `-term` excludes, quoted phrase is contiguous; `scope=body` returns a matching `snippet`; BM25 vs date sort. |

<!-- scenarios:end -->

## Siguiente — documentación

- Vocabulario de métodos (verbos + presets) — [`docs/methods.md`](./docs/methods.md)
  (SSOT en inglés) · [`docs/methods.ru.md`](./docs/methods.ru.md) (espejo en ruso)
- Escenarios de aceptación (32 e2e) — [`docs/scenarios.md`](./docs/scenarios.md)
- Arquitectura y estratificación — [`docs/architecture.md`](./docs/architecture.md)
- Operadores de búsqueda — [`docs/search-operators.md`](./docs/search-operators.md)
- Registro MCP por agente — [`docs/mcp-registration.md`](./docs/mcp-registration.md)
- Cobertura y limitaciones de los parsers — [`docs/parsers.md`](./docs/parsers.md)
- Seguridad (contenido no confiable) — [`docs/security.md`](./docs/security.md)
- Añadir un sexto agente — [`CONTRIBUTING.md`](./CONTRIBUTING.md)

## Desarrollo

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ tests, CI requiere ≥80% de cobertura
- Conventional Commits (`feat:`, `fix:`, `docs:`, …)
- Al añadir nuevos agentes, ver [CONTRIBUTING.md](./CONTRIBUTING.md) y
  [docs/parsers.md](./docs/parsers.md)

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

**Empieza:** clona + `bash install.sh`, luego registra el servidor MCP para tu
agente ([docs/mcp-registration.md](./docs/mcp-registration.md)) y reinicia la
herramienta anfitriona. Una sola superficie de solo lectura hacia el historial de
cada agente.
