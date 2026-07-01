# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> `git` 展示的是**什么**进入了代码。`ai-r` 展示的是**为什么**：哪个智能体
> 做的、依据哪个计划——以及它是否悄悄丢弃了它其实敲定的那个计划。
> 只读，覆盖全部五个编码智能体，一套统一接口。

一个智能体报告：“完成了 X，依照计划 Y。”你却无从核对。计划存在一种格式里，
编辑存在另一种格式里。而如果两个智能体一起做了这个任务，它们的历史根本对不上
——每个都用自己的方式、在自己的地方记录。`ai-r` 读取一个智能体的会话历史，
并从一次编辑背后提取出意图、计划和作者归属。

## 快速示例——一个智能体询问历史

主要模式是 **MCP**：一个智能体（Claude、Codex……）直接调用 `ai-r`，
用自然语言询问历史。例如——取出上一个智能体敲定的计划，草稿已被丢弃：

```
Show me the plan from the last session — final only, no intermediate revisions.
→ ai-r: plan(session=…, kind="final")  →  get_body(id, shallow=true)
        returns the final task + a list of dropped_drafts
```

快速的编辑归属——一条终端命令，一次覆盖每个智能体：

```bash
# who edited this file, and when — cross-agent, optionally time-boxed
ai-r find-file-edits auth.py --since 2026-06-01
```

## 痛点在哪

- “完成了，我按计划 Y 做了 X”——却没有任何东西可以拿来核对：智能体把计划
  存成一种形态、把编辑存成另一种形态。
- 你在任务中途切换了智能体，线索就断了。没有地方可以问“*另一个*智能体
  已经试过什么？”
- 一次编辑出现在某个文件里——却搞不清是**哪个**智能体做的、依据的是什么请求。

一个根源：每个智能体都用**自己的方式**记录历史——Claude 和 Codex 用 JSONL，
OpenCode 用 SQLite，Antigravity 用 “brain” 目录，Pi 用按项目划分的 JSONL。
五种格式、五种布局——放在一起彼此对不上。

## 承诺

`ai-r` 把这五种全部折叠进**一套只读接口**。把任意智能体——或一个脚本、或你
自己——指向任意会话，无论它是由哪个工具记录的。每个智能体一种查询形态；
格式差异在解析器内部被归一化。

## 核心特性

- **不只是“什么”，还有“为什么”。** 提取一次编辑背后的计划、意图与作者归属
  ——而不只是 diff 文本。`git diff` 告诉你*什么*变了；`ai-r` 告诉你依据的是
  哪个计划、由谁的请求触发。
- **是最终计划，不是草稿。** `ai-r` 取出智能体*敲定*的那个计划，并单独展示它
  一路上丢弃了什么（`dropped_drafts`）——覆盖 Claude / Codex / Antigravity，
  尽管它们的计划信号各不相同。
- **跨智能体归属。** 任意文件编辑或工具调用 → 做出它的智能体，外加触发它的
  请求（`find-file-edits` / `find-tool-calls`）。
- **回答短小，正文按需取。** 记录携带指向内容的引用（哈希 + 长度）；完整的
  编辑文本单独获取——响应不会膨胀。
- **通过 MCP 工作（13 个工具）。** 智能体用自然语言直接调用 `ai-r`；同一份
  数据也可从终端（CLI）和代码（Python SDK）获得。
- **是读者，不是守卫。** 它提取实体；由你（或你的工具）来构建知识图谱与记忆。
  只读：它绝不运行、也绝不写入智能体的历史。

## 你用它来做什么

- **用一双全新的眼睛审计会话。** 一个上下文为空的全新智能体从三个维度冷静地
  核查过往会话：承诺和要求是否达成；决策是否稳妥、判断是否得当；问题被探究得
  有多深——智能体遗漏了什么。在一次真实运行中，一周里以此方式复核了 271 次
  对话，抓到了那些完成了任务、**但在规划上误导了你**的智能体——这是实时对话
  会掩盖的，并会把你引向错误的决策。
- **在耗尽的上下文之后继续——而不丢失细节。** `/compact` 抹掉了具体细节。
  取而代之，开一个全新会话：它读取上一次会话的**日志**，并从其结论处继续，
  不必在已经理清的东西上重新烧掉上下文。原始会话保持完好——供审计和搜索。
  新会话可以在**任意**智能体中运行：历史无论用哪个工具都能对上。
- **喂给你的记忆系统。** 保持 Karpathy 式的记忆与摘要，或用你自己的方法？
  `ai-r` 为 AI 聊天提供你已经对消息历史所做的那件事——解析出的实体，用来
  为那些重要的细节建立持久记忆。
- **回想你做了什么、以及为什么。** 这个文件为什么被编辑？这条规则为什么被添加？
  找到文件变更所在的那次会话，读取编辑*之前*的那条请求。

## 它与会话搜索工具有何不同

如今已有少数几个跨智能体工具能读取不止一个智能体的历史
（`jazzyalex/agent-sessions`、`Dicklesworthstone/coding_agent_session_search`、
`hacktivist123/agent-session-resume`）。它们几乎都是关于**搜索与时间线**：
找到一次*会话*，滚动浏览历史。

`ai-r` 走得更深：它把**计划、意图与作者归属提取为现成的实体**，供你在其上
构建记忆。搜索找到的是文本——`ai-r` 回答的是**为什么**。技术上，一个搜索
工具也可以从会话文本里挖出一个计划，但它不会把它解析成单一、归一化的形态
交回给你——用 `ai-r`，那正是主要接口。

| 能力 | 单智能体查看器 | 跨智能体搜索工具 | `ai-r` |
|---|---|---|---|
| 读取多个智能体的日志 | 否 | 是 | 是 —— Claude、Codex、OpenCode、Antigravity、Pi |
| 编程接口 | 多为 GUI/TUI | 多为 TUI/CLI/应用 | **MCP + CLI + Python SDK** |
| 归属（编辑/命令 → 智能体 + 意图） | — | 部分 | 是 —— `find-file-edits` / `find-tool-calls` |
| 审计回放（重建一次会话的变更，无需 git） | — | 很少 | 是 —— `session_diff` |
| 计划提取（最终 vs 草稿，归一化） | — | — | 是 —— `plan` |
| 定位 | 查看器 | 搜索 / 恢复 / 记忆 | **只读提取内核** |

*竞品各列反映的是截至 2026-07 其公开文档的情况；凡某项能力不明确之处，我们宁可保守低估，也不夸大宣称。*

我们刻意**不**在智能体覆盖广度、速度或 TUI 丰富度上竞争。`ai-r` 的切入点是
提取“为什么”以及供机器消费的结构化实体。

## 已在实践中验证

`ai-r` 已经在读取它自己的开发历史——覆盖全部五个智能体。真实的工具运行于其上
（它们独立存在，构建在其只读 API 之上）：

- **auditor** —— 一个全新智能体冷静地核查上一个智能体到底做了什么、决定了什么。
  这抓到了那些悄悄谎报计划的智能体。
- **summarizer**（`export rounds`）—— 把一次会话渲染成一份现成的交接文档。
- **ai-local-reader** —— 一个只读技能：从磁盘审计跨所有智能体的过往会话。

这些工具都在工作流一侧，位于本仓库之外。`ai-r` 本身只读取并返回数据。

## 支持的智能体

| 智能体 | 存储 | 解析器 |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite（snap/flatpak 自动探测） |
| Antigravity | `~/.gemini/antigravity/brain/` | JSON / markdown brain 目录 |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

不是你用的智能体？加入第六个只需**一个解析器模块**；这套只读模式能在几分钟内
移植到任意工具上。参见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 接口

`ai-r` 以三种方式提供相同的读取能力：

- **MCP 服务器**（`ai-r-mcp`）—— 通过 stdio JSON-RPC 提供 13 个工具，任意 MCP
  智能体都能直接调用它（推荐）。注册——参见
  [docs/mcp-registration.md](./docs/mcp-registration.md)。
- **CLI**（`ai-r`）—— 供脚本和手动使用的子命令（`list` / `read` /
  `search` / `find-file-edits` / `find-tool-calls` / `file-frequency` /
  `detect-agent` / `export rounds`）。搜索运算符——
  [docs/search-operators.md](./docs/search-operators.md)。
- **Python SDK**（`from ai_r.parsers import ...`）—— 解析器、带类型的
  `Session`/message 模型，以及事件动词，用来构建你自己的工具。

### 方法词汇表（SSOT）

下方的区块取自 [`docs/methods.md`](./docs/methods.md) —— 公开动词与预设的
英文事实来源。它与该文件的标记区块保持同步。

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

### 事件内核

上面的动词是新的：一个**事件内核**取代了一堆一次性工具。每个解析器读取一个
智能体的日志并发出带类型的模型，归一化进一条单一、智能体无关的流——
`user_turn` / `assistant_turn` / `tool_call(...)` / `plan_event`。一小组动词
对这条流进行过滤、聚合与 diff；智能体之间的差异（`ExitPlanMode` vs
`update_plan` vs `implementation_plan.md`）留在解析器内部——调用方看到的是
一种形态。

一个诚实的边界：这是**仅提取实体**——回合、工具调用、计划、意图、反应。
它**不是**图谱，也**不是**记忆存储。接下来你做什么（知识图谱、Obsidian、
持久记忆）都在你这一侧，位于本仓库之外。完整的分层与 MCP 工具列表，参见
[docs/architecture.md](./docs/architecture.md)。

## 快速开始（1 条命令）

要求：带 `venv` 或 `pip` 的 Python 3.11+，以及 `jq`（用于自动修补 Claude 和
Antigravity 的 MCP 配置——其余的不需要 `jq`）。

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

安装器会创建一个 venv、安装运行时包、为 **Claude**、**Codex**、**OpenCode**、
**Antigravity** 修补 MCP 配置（在配置存在的地方），安装 **Pi** CLI 技能，
并运行冒烟测试。

## 边界：是读者，不是守卫

- **只读。** 它绝不运行智能体的代码，也绝不写入其历史——它只读取并返回。
- **无图谱、无记忆。** 它提取实体（回合、调用、计划、意图）。用它们构建知识
  图谱或记忆是你的活儿，不是它的。
- **不是访问控制层。** 任何能触及 CLI、MCP 服务器或该包的人都能读取任意会话。
  解析器前面没有授权；把它放在不受信任的本地进程够不着的地方。
- **会话内容是数据，不是命令。** 无论谁来读取（auditor、summarizer）都必须
  把会话文本当作数据、而非指令来对待。参见
  [Security](docs/security.md)。

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

## 下一步——文档

- 方法词汇表（动词 + 预设）—— [`docs/methods.md`](./docs/methods.md)
  （英文 SSOT）· [`docs/methods.ru.md`](./docs/methods.ru.md)（俄文镜像）
- 验收场景（32 个 e2e）—— [`docs/scenarios.md`](./docs/scenarios.md)
- 架构与分层 —— [`docs/architecture.md`](./docs/architecture.md)
- 搜索运算符 —— [`docs/search-operators.md`](./docs/search-operators.md)
- 各智能体 MCP 注册 —— [`docs/mcp-registration.md`](./docs/mcp-registration.md)
- 解析器覆盖与限制 —— [`docs/parsers.md`](./docs/parsers.md)
- 安全（不受信任的内容）—— [`docs/security.md`](./docs/security.md)
- 加入第六个智能体 —— [`CONTRIBUTING.md`](./CONTRIBUTING.md)

## 开发

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ 个测试，CI 要求 ≥80% 覆盖率
- Conventional Commits（`feat:`、`fix:`、`docs:`……）
- 加入新智能体时，参见 [CONTRIBUTING.md](./CONTRIBUTING.md) 和
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

## 许可证

MIT —— 参见 [LICENSE](./LICENSE)。

---

**开始上手：** clone + `bash install.sh`，然后为你的智能体注册 MCP 服务器
（[docs/mcp-registration.md](./docs/mcp-registration.md)）并重启宿主工具。
一套只读接口，通向每个智能体的历史。
