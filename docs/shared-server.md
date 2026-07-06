# Shared server: why `ai-r-mcp` can run as one local http process

By default every agent (and every subagent it spawns) starts its **own**
`ai-r-mcp` process over stdio. That is simple and needs zero setup — but under
multi-agent fan-out it does not scale, and on a busy laptop it can wedge the
whole machine. This page explains the failure, the fix, and the measured
before/after — and why "http" here means **local**, not "on the network".

## The problem: a per-agent process swarm

stdio transport = one pipe = one client. So N concurrent agents ⇒ N separate
`ai-r-mcp` processes, and each one:

- holds its **own cold cache** — nothing is shared between processes, so each
  re-reads and re-parses the whole session corpus on its first body search;
- keeps its **own resident memory** the whole time the agent is alive.

A real session audit on the author's machine caught this live: **10**
`ai-r-mcp` instances alive at once, two of them pinned at **20 % CPU**, the
hottest resident **~840 MB** and running for over two hours. Free RAM fell
under 1 GB, the system started swapping, the desktop compositor was starved of
CPU, and the screen showed graphical artifacts and lag.

## The fix: one shared, socket-activated, local server

Set `AI_R_MCP_TRANSPORT=http` and `ai-r-mcp` runs a single long-lived
[streamable-http](https://modelcontextprotocol.io) server that **every** agent
connects to. One process, one warm cache. A systemd `--user` socket unit keeps
a listener on `127.0.0.1:8756` and starts the server on the first connection;
the server exits itself after an idle window and is re-started on demand — so
there are **zero** resident processes when nothing is using it.

```
stdio  (default):   agent1 → ai-r-mcp #1 (cold cache, ~300 MB)
                     agent2 → ai-r-mcp #2 (cold cache, ~300 MB)
                     agent3 → ai-r-mcp #3 (cold cache, ~300 MB)   … N processes

http   (shared):    agent1 ┐
                     agent2 ┼→ 127.0.0.1:8756 → ONE ai-r-mcp (one warm cache)
                     agent3 ┘                    idle → exits, respawns on demand
```

## Measured before / after

Same workload both ways — a full-corpus `scope="body"` search (the exact
operation that dominates the swarm's cost), on a ~1492-session corpus:

| | stdio swarm (before) | http shared (after) |
|---|---|---|
| Processes for N agents | **N** (one per agent) | **1** (shared), 0 when idle |
| Resident memory | ~300 MB **× N** (measured: 4 cold servers ≈ **+1.2 GB**) | ~300 MB, **once** |
| First body scan (cold) | ~70 s CPU, **per process** | ~70 s CPU, **once** |
| Repeat body scan (warm) | ~70 s again (no shared cache) | **~9 s** — corpus served from the warm cache (**~17× faster**) |
| Idle footprint | processes linger with their agents | server self-exits, socket respawns on demand |

The warm-repeat win needs the cache to hold the whole corpus; the cap defaults
to 2048 and is tunable with `AI_R_HAYSTACK_CACHE_MAX` (see
[architecture.md](architecture.md) → *shared http transport* ADR). The
memory win — N processes collapsing to 1 — holds regardless of the cache.

### Under concurrency: the swarm collapses, the shared server stays flat

A second measurement drove a mass-audit workload (per session:
`read_session` + `incidents`) through a pool of concurrent workers, at rising
concurrency. The stdio swarm's memory grows linearly with concurrency and then
**thrashes to a standstill**; the shared server is one process at flat memory.

| Concurrency | stdio swarm (before) | http shared (after) |
|---|---|---|
| 4 workers | 4 procs, **361 MB**, all done | 1 proc, ~**80 MB** |
| 8 workers | 8 procs, **712 MB** | 1 proc, ~**80 MB** |
| 16 workers | 16 procs, **1404 MB**, **0 completed** — CPU-contention collapse (the freeze mechanism) | 1 proc, ~**80 MB** |

The shared server's resident memory is **flat** (~80 MB) no matter how many
sessions or workers — one process, measured not to grow across a 200-session
run. The real freeze incident was ~10 concurrent instances; the swarm column
shows why the machine wedged there.

The honest trade-off: the swarm gets true multi-core parallelism (separate
processes) and is *faster* at low concurrency — but its memory explodes with
concurrency and it seizes up past ~10. The shared server serializes CPU-bound
work through one process (GIL), so raw throughput is lower, but memory stays
bounded and it **never freezes the host** — the right choice for running a mass
re-audit unattended on a laptop, throttled to a small worker pool.

## "http" here is **local**, not the network

This is not a cloud service and nothing leaves your machine:

- The server binds **`127.0.0.1`** (loopback) only — it is not reachable from
  the network, just other processes on the same host. "http" is the local IPC
  protocol between your agents and the reader, nothing more.
- ai-r **makes no outbound network calls** at all. It reads local session
  files and returns data; there is no generative-model call, no telemetry, no
  upload. (The optional semantic re-rank runs a local ONNX model on-device;
  see the *semantic re-rank* ADR.) Your conversations never leave the laptop.

## Enabling it

1. Install with the http extra and the units:
   `AI_R_EXTRAS=http AI_R_MCP_SYSTEMD=1 bash install.sh`
2. Enable the socket: `systemctl --user enable --now ai-r-mcp.socket`
3. Point each agent's MCP config at `http://127.0.0.1:8756/mcp`
   (Claude: `{"type":"http","url":"http://127.0.0.1:8756/mcp"}`;
   OpenCode: `{"type":"remote","url":"http://127.0.0.1:8756/mcp"}`).

stdio stays the default and keeps working — http is strictly opt-in, and
switching one agent does not affect any other. Running sessions keep their
transport until they restart, so nothing breaks mid-session.
