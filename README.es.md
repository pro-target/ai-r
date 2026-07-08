# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> `git` muestra **qué** llegó al código. `ai-r` muestra **por qué**: qué agente
> lo hizo, bajo qué plan — y si silenciosamente descartó el plan que realmente
> había acordado. Solo lectura, a través de los cinco agentes de programación, una
> sola interfaz.

**Ruta de lectura determinista: la extracción no hace llamadas a LLM ni peticiones de red salientes; el re-ranking semántico opcional también es local (embeddings, no un LLM).**

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
- **Funciona sobre MCP (15 herramientas).** Un agente llama a `ai-r` directamente
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

Un puñado de herramientas multiagente ya leen el historial de más de un agente
(`jazzyalex/agent-sessions`, `Dicklesworthstone/coding_agent_session_search`,
`hacktivist123/agent-session-resume`). Casi todas van de **búsqueda y línea de
tiempo**: encontrar una *sesión*, recorrer el historial.

`ai-r` va más profundo: extrae el **plan, la intención y la autoría como entidades
listas para usar** sobre las que construyes memoria. La búsqueda encuentra texto —
`ai-r` responde **por qué**. Técnicamente, una herramienta de búsqueda también
podría desenterrar un plan del texto de una sesión, pero no lo devuelve parseado
en una única forma normalizada — con `ai-r` esa es la superficie principal.

| Capacidad | Visores de un solo agente | Herramientas de búsqueda multiagente | `ai-r` |
|---|---|---|---|
| Lee los logs de >1 agente | No | Sí | Sí — Claude, Codex, OpenCode, Antigravity, Pi |
| Superficie programática | Mayormente GUI/TUI | Mayormente TUI/CLI/app | **MCP + CLI + SDK de Python** |
| Atribución (edición/comando → agente + intención) | — | Parcial | Sí — `find-file-edits` / `find-tool-calls` |
| Replay de auditoría (reconstruir los cambios de una sesión, sin git) | — | Rara vez | Sí — `session_diff` |
| Extracción de plan (final vs borrador, normalizada) | — | — | Sí — `plan` |
| Alcance | Visor | Búsqueda / reanudación / memoria | **Núcleo de extracción de solo lectura** |

*Las columnas de la competencia reflejan su documentación pública a fecha de
2026-07; donde una capacidad no está clara, subestimamos en lugar de exagerar.*

Deliberadamente **no** competimos en amplitud de agentes, velocidad ni riqueza de
la TUI. La ventaja de `ai-r` está en extraer el "por qué" y entidades
estructuradas para consumo por máquinas.

## Probado en la práctica

`ai-r` ya lee su propio historial de desarrollo — a través de los cinco agentes.
Sobre él corren herramientas reales (viven aparte, encima de su API de solo
lectura):

- **auditor** — un agente nuevo revisa fríamente lo que el anterior hizo y decidió
  realmente. Esto pilló a agentes que mintieron discretamente sobre el plan.
- **summarizer** (`export rounds`) — renderiza una sesión en un documento de
  traspaso listo para usar.
- **ai-local-reader** — un skill de solo lectura: audita sesiones pasadas desde
  disco a través de todos los agentes.

Estas herramientas son del lado del flujo de trabajo, fuera de este repo. El
propio `ai-r` solo lee y devuelve datos.

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

- **Servidor MCP** (`ai-r-mcp`) — 15 herramientas sobre JSON-RPC, para
  que cualquier agente MCP lo llame directamente (recomendado). Por defecto es
  **stdio**; opcionalmente un **servidor http compartido** (un único proceso
  caliente para todos los agentes en lugar de un enjambre de stdio por agente),
  ver el extra `http` en Inicio rápido. Registro — ver
  [docs/mcp-registration.md](./docs/mcp-registration.md).
- **CLI** (`ai-r`) — subcomandos para scripts y uso manual (`list` / `read` /
  `search` / `find-file-edits` / `find-tool-calls` / `file-frequency` /
  `detect-agent` / `export rounds`). Operadores de búsqueda —
  [docs/search-operators.md](./docs/search-operators.md).
- **SDK de Python** (`from ai_r.parsers import ...`) — parsers, modelos tipados
  `Session`/mensaje, y los verbos de eventos, para construir tus propias
  herramientas.

### Vocabulario de métodos

El vocabulario completo de verbos y presets públicos (firmas, parámetros y
comportamiento) vive en un archivo aparte: [`docs/methods.md`](./docs/methods.md).

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

**Extra opcional — `tokens`:** `AI_R_EXTRAS=tokens bash install.sh` (o
`pip install "ai-r[tokens]"`) añade [tiktoken](https://github.com/openai/tiktoken)
para mejores **estimaciones** de tokens en sesiones cuyo formato no almacena
cifras de uso exactas. Totalmente opcional: sin él, las cifras exactas siguen
saliendo directamente de los archivos de sesión donde están registradas, y la
estimación de reserva degrada a una heurística aproximada de `chars/4`,
etiquetada honestamente como `estimate` — nunca una caída.

**Extra opcional — `semantic`** (`AI_R_EXTRAS=semantic bash install.sh`
o `pip install "ai-r[semantic]"` + una descarga única del modelo, que el
instalador hace por sí solo): habilita `sort="semantic"` en la búsqueda de texto
(`query`, `search_sessions`) — los 50 mejores candidatos de BM25 se reordenan por
**significado**.

- **Modelo.** Modelo local multilingüe de embeddings
  [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)
  (int8 ONNX, ~118 MB, MIT) — directamente vía
  [onnxruntime](https://onnxruntime.ai) + [tokenizers](https://github.com/huggingface/tokenizers) + [numpy](https://numpy.org),
  sin torch y sin índice persistente. Elegido por su fuerte búsqueda
  cross-lingüe (una consulta en español encuentra una sesión en inglés y
  viceversa) con un tamaño reducido.
- **Cómo se calcula la puntuación.** BM25 selecciona las 50 mejores
  coincidencias por palabras (es un presupuesto de coste, no un corte por
  calidad — a propósito NO hay umbral de similitud: en esta familia de modelos
  incluso textos no relacionados obtienen ≈0.7). Dentro del pool, la puntuación
  final = **75 % significado + 25 % coincidencia de palabras** — el significado
  manda, y la parte léxica evita hundir una aparición exacta del término y
  desempata los empates.
- **Fail-soft.** Sin los paquetes o los archivos del modelo, `sort="semantic"`
  cae de forma honesta al orden BM25 con una explicación
  (`semantic: {active: false, reason, fallback: "bm25"}`) — nunca una caída.

**Extra opcional — `http`** (`AI_R_EXTRAS=http bash install.sh` o
`pip install "ai-r[http]"`): añade [uvicorn](https://www.uvicorn.org) y habilita
un **transporte streamable-http compartido** (requiere `mcp>=1.9.0`).

- **Para qué.** Por defecto cada agente lanza su propio `ai-r-mcp` sobre stdio —
  bajo un fan-out multiagente eso son N procesos, cada uno con una caché fría,
  re-escaneando el corpus (la causa medida del agotamiento de RAM). Con
  `AI_R_MCP_TRANSPORT=http` un único **servidor caliente** en localhost (por
  defecto `127.0.0.1:8756`) es compartido por todos en lugar de un enjambre; las
  unidades de systemd en `packaging/systemd/` dan activación por socket con
  auto-salida por inactividad.
- **Seguridad (fail-closed).** El bind es solo loopback. Los ataques desde el
  navegador (DNS-rebinding) los corta el allowlist de Origin/Host del SDK
  (siempre activo para loopback). El acceso remoto requiere
  `AI_R_MCP_ALLOW_REMOTE=1` **y** el token `AI_R_HTTP_TOKEN` — sin token no
  arranca (los transcripts contienen secretos). En loopback el token es opcional
  (protección frente a otro usuario local de una máquina compartida); el cliente
  envía la cabecera `Authorization: Bearer <token>`.
- **Ajustes (env):**
  - `AI_R_MCP_PORT` — puerto (por defecto `8756`).
  - `AI_R_MCP_IDLE_SEC` — umbral de auto-salida por inactividad.
  - `AI_R_MCP_HOST` / `AI_R_MCP_ALLOW_REMOTE` — host del bind / permitir no-loopback.
  - `AI_R_HTTP_TOKEN` — token bearer (obligatorio para el bind remoto).
  - `AI_R_HAYSTACK_CACHE_MAX` — tope de la caché de búsqueda por número de entradas.
  - `AI_R_HAYSTACK_CACHE_CHARS_MAX` — por volumen total (fusible del RSS de un
    servidor de larga vida).

Ambos extras son totalmente opcionales: sin ellos, el modo stdio y el orden BM25
funcionan como antes.

## Límites: un lector, no un guardián

- **Solo lectura.** Nunca ejecuta el código de un agente ni escribe en su
  historial — lee y devuelve.
- **Sin grafo, sin memoria.** Extrae entidades (turnos, llamadas, planes,
  intenciones). Construir un grafo de conocimiento o memoria a partir de ellas es
  tu trabajo, no el suyo.
- **No es control de acceso — salvo el transporte http.** Quien alcance la CLI,
  el MCP por stdio o el paquete lee cualquier sesión: es el mismo usuario local,
  y una comprobación de permisos delante de los parsers no protegería nada. La
  excepción es el transporte http compartido: al estar accesible por socket,
  lleva allowlist de Origin y un token bearer opcional (obligatorio para el bind
  remoto, ver el extra `http` arriba). Aun así, mantén los datos donde procesos
  locales ajenos no puedan alcanzarlos.
- **El contenido de la sesión es datos, no comandos.** Quien lea (auditor,
  summarizer) debe tratar el texto de la sesión como datos, no como
  instrucciones. Ver [Seguridad](docs/security.md).

## Aceptación (escenarios end-to-end)

La superficie pública está cubierta por escenarios e2e que un agente LLM ejecuta
contra un MCP en vivo (complementan a pytest, no lo reemplazan). La lista completa
está en [`docs/scenarios.md`](./docs/scenarios.md).

<!-- gallery:start -->
## Ejemplo: ai-r en acción

Una galería de ejemplos reales — uno por capacidad (análisis de errores, comandos peligrosos, rastro de red, gasto de tokens, comentarios de plan, verificación de commits fantasma, historial de archivos entre agentes, búsqueda multilingüe, subagentes zombis, diff sin git): [`docs/examples/showcase-gallery.md`](./docs/examples/showcase-gallery.md).
<!-- gallery:end -->

## Siguiente — documentación

- Vocabulario de métodos (verbos + presets) — [`docs/methods.md`](./docs/methods.md)
  (SSOT en inglés) · [`docs/methods.ru.md`](./docs/methods.ru.md) (espejo en ruso)
- Escenarios de aceptación (97 e2e) — [`docs/scenarios.md`](./docs/scenarios.md)
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

- 1100+ tests, CI requiere ≥85% de cobertura
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
