# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> **Una única ventana de solo lectura al historial de sesiones de todos tus
> agentes de IA** — Claude, Codex, OpenCode, Antigravity y Pi — vía MCP, un
> CLI o un paquete de Python.
>
> Cambia de agente sin perder el contexto · Audita lo que cada agente hizo *y
> ejecutó* · Busca en todas las sesiones a la vez.

```bash
# una consulta, todos los agentes — encuentra la sesión donde apareció ese bug de auth
ai-r search "auth token refresh" --scope body
```

## ¿Por qué?

Cada agente de IA para programar guarda su propio historial de conversación —
en su propio lugar, en su propio formato. Claude y Codex escriben JSONL,
OpenCode usa SQLite, Antigravity dispersa directorios "brain", Pi escribe JSONL
por proyecto. Así que tu trabajo queda **aislado por herramienta**: cambias de
agente y pierdes el hilo; no puedes preguntar «¿qué ya intentó el otro agente?».

`ai-r` colapsa todo eso en **una única interfaz de solo lectura**. Apunta
cualquier agente — o un script, o a ti mismo — a cualquier sesión, sin importar
qué herramienta la escribió. Es memoria compartida entre todos los agentes que
usas.

## Prueba — lo uso en mi propio trabajo

`ai-r` lee las mismísimas sesiones que construyeron `ai-r`. A lo largo de
**5 agentes** y **684 sesiones registradas**, se ha llamado **~125 veces**:
49 lecturas de sesión, 37 búsquedas por cuerpo, 31 listados, 9 rastreos de
ediciones de archivos. El uso principal es la **auditoría** — un agente nuevo
cuyo único trabajo es revisar fríamente lo que un agente anterior realmente hizo
y decidió. Eso ha pillado agentes que habían desviado la planificación de forma
silenciosa (y a mí); ahora esos descuidos se detectan.

## Cuándo ayuda

Un único lector sobre todos los agentes desbloquea flujos de trabajo que el
registro de un solo agente no permite:

- **¿Límite del proveedor alcanzado? Cambia de agente y sigue.** ¿Se te agotó la
  cuota de Codex a mitad de la tarea? Levanta Antigravity, apúntalo a la sesión
  de Codex y pídele que continúe — misma tarea, modelo distinto, sin perder
  contexto.
- **¿Ventana de contexto agotada? Empieza de nuevo y reanuda.** Abre una sesión
  nueva, pásale el UUID de la anterior y dile «continúa desde aquí». La
  transcripción previa es legible sin importar qué agente la escribió.
- **Traspaso y triaje entre agentes.** «¿Qué hizo el otro agente con esto?»
  funciona entre Claude, Codex, OpenCode, Antigravity y Pi sin aprender cinco
  diseños de registro distintos.
- **«¿Quién tocó este archivo y cuándo?»** Cada edición a una ruta — en todos los
  agentes, todas las sesiones — con marcas de tiempo. Auditoría por periodo:
  «¿qué le hicieron los agentes a `src/auth.py` la semana pasada?» (ver
  `find-file-edits`).
- **Audita lo que los agentes *ejecutaron*, no solo lo que cambiaron.** Cada
  llamada a herramienta — comandos de shell, escrituras de archivos, peticiones
  web, llamadas MCP — en todos los agentes, cada una etiquetada con la petición
  del usuario que la disparó. «¿Algún agente ejecutó un deploy la semana
  pasada?» «Muéstrame cada comando de shell que ejecutó Codex.» (ver
  `find-tool-calls`).
- **Reproduce una sesión como ronda de CHANGELOG.** Renderiza una sesión en
  markdown Objetivo / Estado / Archivos tocados / Decisiones / Próximos pasos —
  un documento de traspaso que puedes pegar en otro agente o en un standup (ver
  `export rounds`).
- **«¿Qué agente soy y en qué sesión estoy?»** Un script o un agente que arranca
  de nuevo detecta su propio UUID de sesión, luego se lee a sí mismo o a su
  predecesor para reanudar de forma programática (ver `detect-agent`,
  `detect-session`).
- **Recupérate tras un crash.** ¿Se murió el agente, se cerró la terminal, se
  reinició la máquina? Detecta la sesión en la que estabas, vuelve a leerla y
  retoma exactamente donde se quedó — no se perdió nada (ver `detect-session`).
- **Busca en los cuerpos, no solo en los títulos.** «Encuentra todas las
  sesiones que discutieron `auth token`» — en los cinco agentes, con snippets —
  mediante búsqueda con alcance de cuerpo y modos `operator` (`AND`/`OR`/`NOT`).
  Perfecto para «¿ya he resuelto esto antes?» — encuentra la sesión pasada y
  reutiliza la solución en vez de rehacerla.

## Inicio rápido (1 comando)

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

## Arquitectura

```
┌──────────────────────────────────────────────────────────────┐
│ Layer 1: Public API (3 surfaces)                             │
│   • ai-r CLI (argparse)                                 │
│   • ai-r-mcp (MCP server, stdio JSON-RPC)               │
│   • from ai_r.parsers import ...  (Python SDK)          │
└──────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Layer 2: Core (parsers/, models)                             │
│   • claude, codex, opencode (SQLite), antigravity, pi        │
│   • Auto-detect snap/flatpak OpenCode DBs                    │
└──────────────────────────────────────────────────────────────┘
```

## También un núcleo reutilizable, no solo una herramienta

`ai-r` está hecho para que lo tomes prestado. Los parsers, los modelos tipados
`Session` y de mensajes, y los helpers de seguridad (manejo de contenido no
confiable, topes de tamaño, escaneo de shell con conocimiento de comillas) son
pequeños, ligeros en dependencias y de solo lectura por diseño. ¿No ves tu
agente arriba? Toma el kernel, apúntalo a un nuevo formato de registro y ya
tienes un lector para él — la mayoría de los agentes están a un módulo de parser
de distancia. El repo entero funciona además como una plantilla de trabajo para
«leer el historial de todos los agentes, de forma segura».

## Límites de diseño

`ai-r` es el núcleo público: parsers, mensajes tipados, CLI y MCP. Los
revisores, resumidores y auditores específicos de cada flujo viven fuera de este
repo y consumen la API del parser (`read_messages`).

`ai-r` es un **lector, no un guardia.** Cualquier llamador que pueda alcanzar el
CLI, el servidor MCP o el paquete puede leer cualquier sesión — no hay capa de
control de acceso frente a los parsers. Mantenlo donde llamadores locales no
confiables no puedan alcanzarlo.

El contenido de las sesiones **no es confiable** — el llamador de un lector
(auditor, resumidor, agente de replay) debe tratarlo como datos, no como
instrucciones. Ver [Seguridad — contenido de sesión no confiable](docs/security.md).

## Limitaciones conocidas

- **Antigravity** — cobertura con fixtures más pruebas de humo opcionales con
  datos reales cuando existe un directorio brain local.
- **Ediciones shell del Codex CLI** — `find_file_edits` recupera escrituras de
  archivos de codex desde comandos shell `exec_command` / `local_shell_call`
  mediante un escaneo conservador de redirecciones con conocimiento de comillas
  (`>` / `>>`). Las escrituras hechas con `tee` / `sed -i` / `cp` / `mv` / solo
  heredoc no se detectan; las ediciones estructuradas (`apply_patch` /
  `write_file`) siempre se detectan.

La matriz completa de cobertura de parsers está en [docs/parsers.md](docs/parsers.md).

## Uso

### Como servidor MCP (recomendado)

El servidor MCP se registra automáticamente en la configuración de tu agente.
Herramientas disponibles:

| Herramienta | Propósito |
|---|---|
| `list_sessions(agent?, limit?, offset?)` | Lista sesiones descubribles, opcionalmente filtradas por agente. Paginación: `limit=0` = sin tope; la respuesta trae `total`/`offset`/`limit`/`truncated`. |
| `read_session(uuid, agent, offset?, limit?)` | Lee una sesión; devuelve hasta 100 mensajes por defecto. Pasa `offset`/`limit` para paginar más. |
| `find_file_edits(path, agent?, since?, until?, limit?)` | Encuentra cada edición de archivo de una ruta en todas las sesiones, entre agentes por defecto, opcionalmente por periodo (`since`/`until` ISO 8601). |
| `find_tool_calls(tool_name?, tool_name_pattern?, agent?, since?, until?, limit?)` | Encuentra cada llamada a herramienta — comandos de shell, escrituras de archivos, peticiones web, llamadas MCP — en todas las sesiones. Coincide con un nombre de herramienta exacto (`tool_name`) o por subcadena (`tool_name_pattern`); entre agentes y acotable por periodo. Cada coincidencia trae la petición del usuario que la disparó en `intent`. |
| `search_sessions(query, agent?, scope?, operator?, limit?)` | Busca por título y/o cuerpo de mensajes, con modo `operator` (`AND`/`OR`/`NOT`) y exclusiones estilo Google `-term`. Ver [Operadores de búsqueda](#operadores-de-búsqueda). |

**Paginación** (`limit`/`offset`, más un flag `truncated` cuando quedan más
páginas) está expuesta en las herramientas MCP y en el SDK de Python — ver
[architecture.md](docs/architecture.md).

### Como CLI (pruebas / scripts)

```bash
# list / read / search
ai-r list --agent pi
ai-r read --agent pi <session-uuid>
ai-r search "refactor"
ai-r search "pwa manifest" --scope body --operator and --agent claude

# quién editó un archivo, en todos los agentes, opcionalmente por periodo
ai-r find-file-edits src/auth.py --since 2026-06-01 --until 2026-06-30
ai-r find-file-edits "config" --agent claude --limit 20

# ¿qué ejecutaron los agentes? nombre de herramienta exacto o patrón por subcadena, por periodo
ai-r find-tool-calls Bash --since 2026-06-01
ai-r find-tool-calls --pattern deploy --agent codex

# qué agente soy / en qué sesión estoy (scripts, orquestación, auto-reanudación)
ai-r detect-agent --quiet          # → p. ej. "claude"
ai-r detect-session --json         # → UUIDs de sesión candidatos

# renderiza una sesión como ronda de CHANGELOG (doc de traspaso / replay)
ai-r export rounds <session-uuid> --include-round --output round.md
```

Añade `--json` a la mayoría de subcomandos para salida legible por máquina.

### Operadores de búsqueda

`search_sessions` (MCP) y `ai-r search` (CLI) comparten el mismo parser de
consulta y el mismo parámetro operator. El comportamiento por defecto
(`scope="title"`, `operator="AND"`, `limit=50`) no cambia respecto a la búsqueda
previa por subcadena solo en título.

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
hasta 200 caracteres.

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
# solo título (legacy, sigue siendo el predeterminado)
ai-r search "refactor"

# búsqueda por cuerpo, todos los términos deben aparecer, excluir claude
ai-r search "pwa manifest -claude" --scope body --operator and

# búsqueda por cuerpo, cualquier término, máximo 5 resultados
ai-r search "pwa manifest" --scope body --operator or --limit 5

# todo lo que no contenga ninguno de estos términos
ai-r search "auth login" --scope body --operator not
```

### Como SDK de Python

```python
from ai_r.parsers import AgentName, claude

for session in claude.list_sessions():
    print(session.uuid, session.title)

session = claude.read_session("<session-uuid>")
print(session.message_count)

messages = claude.read_messages("<session-uuid>")
print(messages[0].role, messages[0].text)
```

La estratificación completa está en [docs/architecture.md](./docs/architecture.md).

## Registro MCP

`ai-r-mcp` es un servidor MCP stdio. Regístralo una vez por herramienta anfitriona.
Sustituye `USER` por tu nombre de usuario (o quita la ruta absoluta si `ai-r-mcp`
está en tu `PATH`). **Reinicia la herramienta anfitriona tras editar su
configuración** — ninguna recoge cambios MCP en vivo.

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
slash `/ai-r`, pon `enableSkillCommands: true` en
`~/.pi/agent/settings.json` (el texto de la skill funciona incluso con el
`false` por defecto).

### Notas

- `ai-r-mcp` debe estar en `PATH`, o usa la ruta absoluta como arriba.
- El parcheo de configuración JSON usa `jq`. Si falta `jq`, los registros de
  Codex, OpenCode y Pi igual se completan; las configuraciones de Claude y
  Antigravity se omiten — instala `jq` o regístralas a mano con los fragmentos
  de arriba.
- Reinicia la herramienta anfitriona tras editar su archivo de configuración.
- El servidor es de solo lectura; cualquier llamador que lo alcance puede leer
  cualquier sesión. Ver [Límites de diseño](#límites-de-diseño).

## Desarrollo

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ pruebas, cobertura ≥80% requerida por CI.
- Conventional Commits (`feat:`, `fix:`, `docs:`, …).
- Ver [CONTRIBUTING.md](./CONTRIBUTING.md) y [docs/parsers.md](./docs/parsers.md)
  para añadir nuevos agentes.
- `src/ai_r/validators/` y `src/ai_r/templates/` son helpers standalone
  opcionales (validación de markdown session-note), no son parte de la
  superficie del CLI o MCP.

## Licencia

MIT — ver [LICENSE](./LICENSE).
