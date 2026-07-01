# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> **一扇只读的窗口,纵览每个 AI 编码智能体的会话历史** ——
> Claude、Codex、OpenCode、Antigravity、Pi 全覆盖,经由 **MCP**、**CLI**
> 或 **Python SDK** 访问。
>
> 切换智能体而不丢线索 · 把任意一次编辑或命令归因到执行它的智能体 · 回放一段
> 会话 · 提取工作背后的规划 —— 横跨全部五个智能体,统一一个接口。

```bash
# 一次查询,横扫所有智能体 —— 找到当初提到那个 auth bug 的会话
ai-r search "auth token refresh" --scope body
```

## 痛点:五座孤岛,没有共享视图

每个 AI 编码智能体都自己存一份对话历史 —— 各放各的地方,各用各的格式:

- **Claude** 和 **Codex** 写 JSONL,
- **OpenCode** 用 SQLite 数据库,
- **Antigravity** 把 "brain" 目录撒得到处都是,
- **Pi** 按项目写 JSONL。

五种格式,五种布局。于是,只要你跑不止一个智能体,你的工作就会**按工具被孤立**。
换个智能体,你就丢了线索。你没法问一句"*另一个*智能体已经试过什么了?"而当一次
提交或一次文件编辑冒出来时,也没有直截了当的答案说清**究竟是哪个智能体做的**
—— 归因藏在五份互不兼容的日志里,你得一个个去学。

## 承诺

`ai-r` 把这五者收拢成**一个只读接口**。让任意智能体 —— 或一段脚本、或你自己 ——
指向任意会话,无论它是哪个工具写的。对每个智能体,查询的形态都一样;各格式之间
的差异在解析器内部被抹平。

## 工作原理

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

每个解析器读取某一个智能体的磁盘日志,产出带类型的 `Session` 与消息模型。它们
归一化成一条统一的、智能体中立的**事件流** —— `user_turn` / `assistant_turn` /
`tool_call(...)` / `plan_event` —— 再由一小组**动词(verbs)**对这条流进行过滤、
聚合与比较。各智能体之间的差异(`ExitPlanMode` 对 `update_plan` 对
`implementation_plan.md`)藏在解析器内部;调用方看到的只有一种形态。

## 实证 —— 它读的正是造出它自己的那些会话

`ai-r` 读的,正是当初造出 `ai-r` 的那些会话。横跨 **5 个智能体**,它被真实的
消费方例行调用 —— 这些消费方就活在解析器 API 之上:

- **session-summarizer** / `export rounds` —— 把一段会话渲染成 CHANGELOG 风格的
  交接文档。
- **git-log-auditor** —— 一个全新的智能体,唯一职责就是冷眼复盘上一个智能体
  到底做了什么、决定了什么。它已经逮住过几个悄悄把规划带偏的智能体。
- **ai-local-reader** —— 一个只读技能,从本地磁盘审计横跨全部五个智能体的历史
  会话。
- **MCP 注册** —— 服务器会自动注册进 Claude、Codex、OpenCode 和 Antigravity;
  Pi 得到一个 CLI 技能。

这些消费方位于**工作流侧**,活在本仓库之外;它们调用 `ai-r` 的只读解析器 API
(`read_messages`、MCP 工具、动词)。`ai-r` 自身始终只是一个读取器。

## 快速开始(1 条命令)

前置要求:Python 3.11+,带 `venv`(`python3-venv`)或 `pip`
(`python3-pip`/`pip3`),以及 `jq`(用于自动注册 Claude 和 Antigravity 的 MCP
配置 —— 其余的无需 `jq`)。

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

就这样。安装器会:
- 默认使用按用户(per-user)模式;`opt` 模式需显式指定
- 创建 venv,安装运行时包
- 当对应配置文件存在时,为 **Claude**、**Codex**、**OpenCode**、**Antigravity**
  修补 MCP 配置
- 当不存在时,把 **Pi** 的 CLI 技能装到 `~/.agents/skills/ai-r/SKILL.md`
- 跑冒烟测试

## 支持的智能体

| 智能体 | 存储 | 解析器 |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite(自动检测 snap/flatpak) |
| Antigravity | `~/.gemini/antigravity/brain/` | JSON / markdown brain 目录 |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

没有你用的智能体?加上第六个只需**一个解析器模块** —— 这套只读模式能在几分钟
内移植到任意工具(Cursor、Cline、你自己的)。见
[CONTRIBUTING.md](./CONTRIBUTING.md)。

## 表面(Surfaces)

`ai-r` 以三种方式暴露同样的读取能力:

- **MCP 服务器**(`ai-r-mcp`)—— 经由 stdio JSON-RPC 暴露 13 个工具,任何支持
  MCP 的智能体都能直接调用(推荐)。
- **CLI**(`ai-r`)—— 面向脚本与手动使用的子命令。
- **Python SDK**(`from ai_r.parsers import ...`)—— 解析器、带类型的
  `Session`/消息模型,以及事件动词,供你构建自己的工具。

### 方法词表(SSOT)

下方区块取自 [`docs/methods.md`](./docs/methods.md) —— 公共动词与预设的唯一真相
来源。它与该文件的标记区块保持同步。

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

### 这个分支新增了什么 —— 事件内核

上面的动词是新的:一个**事件流内核**取代了一堆一次性工具。要点:

- **`query`** —— 主力。按 `type` / `agent` / `session` / 日期 / `file` / `tool` /
  `text` 过滤统一事件流。设 `sort="relevance"` 时,文本匹配按 BM25 排序(与
  `search_sessions` 同一个打分器)。用 `relative_to`+`direction`+`n` 时,它会走
  相邻的对话轮 —— 这是 `intent` 和 `reaction` 背后的原语。
- **`intent` / `reaction` 预设** —— `intent(event)` = 一个事件*背后*的用户请求
  (往回走);`reaction(event)` = 助手轮*之后*的用户回应(往前走 —— 批评、
  纠正、认可)。
- **`plan`** —— 每个会话按任务分组的归一化规划原子,标注 `final` 对 `draft` 对
  `completed_major`。于是你能提取出*智能体最终定下的那份规划*与被丢弃的修订版本
  —— 横跨 Claude、Codex 和 Antigravity,尽管它们的规划信号各不相同。
  `get_body(..., shallow=True)` 只把最终规划交给子智能体,草稿省略。
- **`aggregate` / `diff` / `detect_current`** —— 通用汇总、按文件拼接的 diff,
  以及运行时自我识别。`session_stats` 和 `session_diff` 如今是这些之上的轻量预设,
  其输出在真实数据上被证明逐字节一致(见上面的 SSOT 区块)。

诚实的范围界定:这是**只读的实体提取** —— 对话轮、工具调用、规划、意图、反应。
它**不是**图谱,也不是记忆库。消费方接下来怎么做(拆进知识图谱、Obsidian、
持久记忆)被刻意**排除在范围之外**,交由消费方侧处理。

### MCP 工具

MCP 服务器暴露 13 个工具。读取的核心几个:

| 工具 | 用途 |
|---|---|
| `list_sessions(agent?, limit?, offset?)` | 列出可发现的会话,可选按智能体过滤。分页。 |
| `read_session(uuid, agent, offset?, limit?)` | 读取单个会话;默认最多 100 条消息,`offset`/`limit` 翻页。 |
| `find_file_edits(path, agent?, since?, until?, limit?)` | 某路径的每一次文件编辑,默认跨智能体,可选按时间段框定。 |
| `find_tool_calls(tool_name?, tool_name_pattern?, agent?, since?, until?, limit?)` | 每一次工具调用 —— shell、文件写入、网页抓取、MCP 调用 —— 每条都带上触发它的用户请求,字段为 `intent`。 |
| `search_sessions(query, agent?, scope?, operator?, limit?, sort?)` | 按标题和/或正文搜索,带 `AND`/`OR`/`NOT` 及 Google 风格的 `-term`;`sort=relevance`(BM25)或 `date`。 |
| `session_stats(agent?, since?, until?, group_by?, top?)` | 按 `agent`/`dir`/`date`/`kind` 对会话分组并排名。 |
| `session_diff(session_uuid, agent, path?)` | 无需 git,按文件重建一段会话改动了什么。 |
| `query`, `plan`, `get_body`, `aggregate`, `diff`, `detect_current` | 上文描述的事件内核动词。 |

**分页**(`limit`/`offset`,以及还有更多页时的 `truncated` 标志)在 MCP 工具
与 Python SDK 上都暴露 —— 见 [architecture.md](docs/architecture.md)。

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

大多数子命令加 `--json` 可得到机器可读输出。事件内核动词
(`query`/`plan`/`aggregate`/`diff`/`detect_current`)在 MCP 和 Python SDK 上
可用;CLI 覆盖上面列出的子命令。

#### 搜索运算符

`search_sessions`(MCP)与 `ai-r search`(CLI)共用同一个查询解析器和同一个
operator 参数。默认行为(`scope="title"`、`operator="AND"`、`limit=50`)是历史上
仅按标题子串搜索的行为。

**查询语法**

| 形式 | 示例 | 含义 |
|---|---|---|
| 裸词 | `pwa manifest` | 两个词都出现(由 operator 控制如何组合)。 |
| 带引号短语 | `"exact phrase"` | 单个字面词。 |
| 负向前缀 | `-claude` | Google 风格:这个词必须不出现。 |

查询中的 `AND`、`OR`、`NOT` 是字面搜索词。布尔行为由 `--operator and|or|not`
(CLI)或 `operator="AND"|"OR"|"NOT"`(MCP)选择。

**operator 模式**(控制正向词如何组合)

| 模式 | `pwa manifest` 语义 | `pwa -claude` 语义 |
|---|---|---|
| `AND`(默认) | 两个都必须出现 | `pwa` 出现,`claude` 不出现 |
| `OR` | 至少一个出现 | 某个 `pwa` 出现,`claude` 不出现 |
| `NOT` | 两个都不出现 | `pwa` 和 `claude` 都不出现 |

**scope 模式**

| Scope | 在哪搜 |
|---|---|
| `title`(默认) | 仅 `session.title` —— 与历史的仅标题行为一致。 |
| `body` | 每个会话的消息文本 + `tool_use[*].input` + `tool_result[*].content`。 |
| `all` | 标题或正文。 |

当 `scope` 为 `body` 或 `all` 且命中时,结果含一个 `snippet` 字段(CLI:打印在
表格里)—— 第一个命中片段,最多 200 字符。结果默认按 BM25 排序
(`sort=relevance`);传 `sort=date` 按时间新旧排序。

**性能提示**:`body` 和 `all` 会对每个候选会话调用 `read_messages`。在大型库里,
首次运行可能较慢;调高 `--limit` 以在迭代时保持结果集有界。

**MCP 示例**

```python
search_sessions(
    query='pwa -claude',
    agent='claude',
    scope='body',
    operator='AND',
    limit=20,
)
```

**CLI 示例**

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

完整的分层见 [docs/architecture.md](./docs/architecture.md)。

## 用例 —— 一个真实消费方,一份活儿

一个覆盖所有智能体的读取器,解锁了单个智能体日志做不到的工作:

- **跨智能体归因 —— "这是哪个智能体干的?"** 对某路径的每一次编辑、每一次工具
  调用,跨每个智能体、每个会话,都带上触发它的请求。按时间段框定:"上周智能体
  对 `src/auth.py` 做了什么?" —— `find-file-edits` / `find-tool-calls`。它驱动了
  **git-log-auditor**。
- **审计与回放 —— 冷眼复盘智能体究竟做了什么。** 一个全新的智能体读取先前的
  会话,报告它*跑过*什么,而不只是声称过什么。`session_diff` 无需 git 就重建按
  文件的改动;`export rounds` 渲染出 CHANGELOG 风格的交接文档。它驱动了
  **session-summarizer** 和 **ai-local-reader**。
- **续接与交接 —— 任务进行到一半换智能体,不丢线索。** 撞到服务商配额,或用完了
  上下文窗口?启动一个新会话(任意智能体),把上一个会话的 UUID 交给它,继续做。
  无论之前是哪个工具写的,旧对话都可读 —— `read_session`、`detect-session`。
- **查找文件编辑 + 意图 —— 这个文件为什么老在变?** `file-frequency` 汇总出哪些
  文件改动最频繁,按编辑次数、不同会话数、不同请求数、涉及的智能体数排名;每一次
  编辑都带上它背后的用户请求,字段为 `intent`。
- **规划提取 —— 找回智能体最终定下的那份规划。** `plan` 按任务返回归一化的规划
  原子,`final` 对 `draft`,横跨 Claude / Codex / Antigravity。用
  `get_body(..., shallow=True)` 只把最终规划交给子智能体。

## 相较于替代品的差异

*于 2026-07-01 经 WebSearch 验证。* 单智能体查看器领域很拥挤
(claude-code-viewer、claude-code-history-viewer、claude-session-viewer、
simonw/claude-code-transcripts、claude-view);少数较新的工具*确实*是跨智能体的
(jazzyalex/agent-sessions、Dicklesworthstone/coding_agent_session_search、
hacktivist123/agent-session-resume)。`ai-r` 的不同之处:

| 能力 | 单智能体查看器 | 跨智能体会话工具 | `ai-r` |
|---|---|---|---|
| 读取多于 1 个智能体的日志 | 否 | 是 | 是 —— Claude、Codex、OpenCode、Antigravity、Pi |
| 程序化表面 | 多为 GUI/TUI | 多为 TUI/CLI/应用 | **MCP + CLI + Python SDK** |
| 归因(编辑/命令 → 智能体 + 意图) | —— | 部分(部分带溯源) | 是 —— `find-file-edits` / `find-tool-calls`,每条都带 `intent` |
| 审计回放(无需 git 重建会话改动) | —— | 少见 | 是 —— `session_diff` |
| 规划提取(final 对 draft,归一化) | —— | —— | 是 —— `plan` |
| 范围 | 查看器 | 搜索 / 续接 / 记忆 | **只读提取内核**(图谱/记忆留给消费方) |

一些跨智能体工具走的是*另一个*方向 —— 朝持久记忆或协调层去(例如
`cass_memory_system`、`mcp_agent_mail`)。`ai-r` 刻意止步于只读提取:记忆与图谱
在消费方侧,不内置。凡竞品的确切能力从公开文档中不明的地方,上表宁可低估,不作
过度声称。

## 设计边界 —— 是读取器,而非守卫

- **只读。** `ai-r` 从不执行智能体代码,也从不写入智能体的会话存储。它只读取
  并返回。
- **无图谱、无记忆。** 它提取实体(对话轮、工具调用、规划、意图)。在其之上构建
  知识图谱或持久记忆,是消费方的活儿,超出本仓库范围。
- **不是访问控制层。** 任何能访问到 CLI、MCP 服务器或该包的调用方,都可以读取
  任意会话 —— 解析器之前没有授权。请把它放在不可信的本地调用方够不着的地方。
- **会话内容是不可信的。** 读取器的调用方(审计器、摘要器、回放智能体)必须把
  会话内容当作*数据,而非指令*。见 [安全 —— 不可信的会话内容](docs/security.md)。

面向特定工作流的审阅者、摘要与审计位于本仓库之外,消费解析器 API
(`read_messages`)。

### 已知限制

- **Antigravity** —— 测试夹具(fixture)覆盖,加上本地存在 brain 目录时可选的
  真实数据冒烟测试。
- **Codex CLI 的 shell 编辑** —— `find_file_edits` 通过保守的、感知引号的重定向
  扫描(`>` / `>>`),从 `exec_command` / `local_shell_call` shell 命令中恢复
  codex 的文件写入。通过 `tee` / `sed -i` / `cp` / `mv` / 仅 heredoc 的写入检测
  不到;结构化编辑(`apply_patch` / `write_file`)始终可检测。

完整的解析器覆盖矩阵见 [docs/parsers.md](docs/parsers.md)。

## MCP 注册

`ai-r-mcp` 是一个 stdio MCP 服务器。每个宿主工具注册一次。把 `USER` 换成你的
用户名(若 `ai-r-mcp` 在 `PATH` 上,也可去掉绝对路径)。**编辑宿主工具的配置后
请重启它** —— 它们都不支持热加载 MCP 变更。

下方代码片段用 `/home/USER/.local/bin/ai-r-mcp`。若你的装在别处,请调整路径
(`which ai-r-mcp` 可查)。

### Claude Code

编辑 `~/.claude.json`(顶层 `mcpServers` 对象):

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

若要单项目注册,在仓库根提交一个 `.mcp.json`(见 [`.mcp.json`](./.mcp.json))。

### Codex

编辑 `~/.codex/config.toml`:

```toml
[mcp_servers.ai-r]
command = "/home/USER/.local/bin/ai-r-mcp"
args = []
```

### Gemini CLI

编辑 `~/.gemini/settings.json`(`mcpServers` 对象):

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

编辑 `~/.config/opencode/opencode.jsonc`(顶层 `mcp` 对象)。OpenCode 与其他工具
有三处不同:`type` 是 `"local"`(非 `"stdio"`),`command` 是一个合并数组(命令 +
参数在一起),环境键是 `"environment"`。

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

编辑 `~/.gemini/antigravity/mcp_config.json`(`mcpServers` 对象)。这与上面
Gemini CLI 的配置不同 —— Antigravity 把自己的 MCP 配置放在
`~/.gemini/antigravity/`。

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

### Pi —— 是技能,不是 MCP

Pi(`@earendil-works/pi-coding-agent`)**没有 MCP 服务器配置可编辑**。它用扩展/
技能模型(`pi install <source>`、`pi config`),而非 `mcpServers` 映射,所以
`ai-r-mcp` 无法作为 Pi 的进程内 MCP 工具注册(而在进程内启动它会违反 Pi 的设计
契约)。取而代之,`install/agent-configs.sh` 把一个只读 **CLI 技能**放到
`~/.agents/skills/ai-r/` —— 一个 Pi 已在扫描的目录。该技能教模型从 Pi 的 bash
会话里调用 `ai-r` CLI,不涉及 MCP 启动。Pi 会话也完全可*由* `ai-r` 通过 CLI
(`ai-r list --agent pi`、`ai-r read …`)或 Python SDK 读取;两者都直接读取
`~/.pi/agent/sessions/` 文件。要用 `/ai-r` 斜杠命令,在
`~/.pi/agent/settings.json` 里设 `enableSkillCommands: true`(即便默认 `false`,
技能文本也能用)。

### 注意事项

- `ai-r-mcp` 必须在 `PATH` 上,否则用上面的绝对路径。
- JSON 配置修补用 `jq`。若缺 `jq`,Codex、OpenCode、Pi 的注册仍会完成;Claude
  与 Antigravity 的配置会被跳过 —— 装 `jq` 或用上面的代码片段手动注册。
- 编辑配置文件后重启宿主工具。
- 服务器只读;任何能访问它的调用方都能读取任意会话。见
  [设计边界](#设计边界--是读取器而非守卫)。

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

## 开发

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ 测试,CI 要求覆盖率 ≥80%
- Conventional Commits(`feat:`、`fix:`、`docs:`、…)
- 关于添加新智能体,见 [CONTRIBUTING.md](./CONTRIBUTING.md) 和
  [docs/parsers.md](./docs/parsers.md)
- `src/ai_r/validators/` 与 `src/ai_r/templates/` 是可选的独立辅助件
  (session-note markdown 校验),不属于 CLI 或 MCP 表面。

<details>
<summary>关键词</summary>

claude code session reader · claude code session parser · codex session parser ·
opencode session reader · antigravity brain parser · pi agent session reader ·
cross-agent attribution · ai coding agent audit · ai agent session history ·
mcp session tools · read-only session reader · agent session replay ·
resume agent session · agent handoff · plan extraction · tool-call audit ·
file edit attribution · multi-agent coding · claude codex opencode antigravity pi

</details>

## 许可证

MIT —— 见 [LICENSE](./LICENSE)。

---

**开始使用:** clone + `bash install.sh`,然后为你的智能体注册 MCP 服务器
([Claude](#claude-code) · [Codex](#codex) ·
[OpenCode](#opencode) · [Antigravity](#antigravity) · [Pi](#pi--是技能不是-mcp))
并重启宿主工具。一扇只读的窗口,纵览每个智能体的历史。
