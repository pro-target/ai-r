# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> `git` はコードに**何が**入ったかを示します。`ai-r` は**なぜ**を示します。
> どのエージェントが、どのプランのもとで行ったのか、そして実際に落ち着いた
> プランをこっそり取り下げていないか。読み取り専用、5 つのコーディング
> エージェントすべてを横断する単一のインターフェースです。

エージェントはこう報告します。「X を完了、プラン Y のとおりに」。あなたには
確かめる手立てがありません。プランはある形式で、編集は別の形式で残ります。
そして 2 つのエージェントがタスクに関わった場合、その履歴はまったく突き合わ
せられません。それぞれが自分のやり方で、自分の場所に書くからです。`ai-r` は
エージェントのセッション履歴を読み取り、ある編集の背後にある意図・プラン・
作者性を取り出します。

## クイック例 — エージェントが履歴について尋ねる

主要なモードは **MCP** です。エージェント（Claude、Codex、…）が `ai-r` を
直接呼び出し、履歴について平易な言葉で尋ねます。たとえば — 前のエージェントが
落ち着いたプランを取り出し、破棄されたドラフトは除きます。

```
Show me the plan from the last session — final only, no intermediate revisions.
→ ai-r: plan(session=…, kind="final")  →  get_body(id, shallow=true)
        returns the final task + a list of dropped_drafts
```

高速な編集の作者特定 — 1 つのターミナルコマンドで、すべてのエージェントを
一度に横断します。

```bash
# who edited this file, and when — cross-agent, optionally time-boxed
ai-r find-file-edits auth.py --since 2026-06-01
```

## 何が困るのか

- 「完了、プラン Y のとおりに X をやりました」— なのに突き合わせる術がない。
  エージェントはプランをある形で、編集を別の形で保持します。
- タスクの途中でエージェントを切り替え、流れを見失った。「*もう一方の*
  エージェントは何をすでに試したのか」を尋ねる場所がありません。
- ある編集がファイルに現れる — なのに**どの**エージェントが、どんな依頼で
  行ったのかがはっきりしません。

原因の一つ: どのエージェントも履歴を**自分のやり方で**書きます。Claude と
Codex は JSONL、OpenCode は SQLite、Antigravity は「brain」ディレクトリ、Pi は
プロジェクトごとの JSONL。5 つの形式、5 つのレイアウト — 合わせると突き合わせ
られません。

## 約束するもの

`ai-r` は 5 つすべてを**単一の読み取り専用インターフェース**へ畳み込みます。
どのエージェント — あるいはスクリプト、あなた自身 — を、どのツールが記録した
ものであれ、任意のセッションに向けられます。エージェントごとに 1 つのクエリ
形状。形式の違いはパーサーの内部で正規化されます。

## 主な機能

- **「何を？」だけでなく「なぜ？」。** 編集の背後にあるプラン・意図・作者性を
  取り出します — 差分テキストだけではありません。`git diff` は*何が*変わったか
  を伝えます。`ai-r` はどのプランのもとで、誰の依頼で行われたかを伝えます。
- **ドラフトではなく最終プラン。** `ai-r` はエージェントが*落ち着いた*プランを
  取り出し、途中で捨てたもの（`dropped_drafts`）を別に示します — プランの
  シグナルが異なる Claude / Codex / Antigravity を横断して。
- **エージェント横断の作者特定。** 任意のファイル編集やツール呼び出し → それを
  行ったエージェントと、それを引き起こした依頼（`find-file-edits` /
  `find-tool-calls`）。
- **小さな回答、本文はオンデマンド。** レコードは内容への参照（ハッシュ + 長さ）
  を保持します。編集全文は別途取得します — 応答が膨れ上がりません。
- **MCP 経由で動作（13 ツール）。** エージェントが `ai-r` を平易な言葉で直接
  呼び出します。同じデータはターミナル（CLI）からもコード（Python SDK）からも
  利用できます。
- **リーダーであって、ガードではない。** エンティティを取り出します。ナレッジ
  グラフとメモリはあなた（またはあなたのツール）が構築します。読み取り専用で、
  エージェントの履歴を実行したり書き込んだりは決してしません。

## 何に使うのか

- **新鮮な目でセッションを監査。** 空のコンテキストを持つ新しいエージェントが、
  過去のセッションを 3 つの軸で冷静にチェックします。約束や要件は満たされたか、
  判断は健全で的確か、問いはどこまで深く掘り下げられたか — エージェントは何を
  見落としたか。ある実際の実行では、この方法で 1 週間に 271 件の対話がレビュー
  され、タスクは完了したが**プランニングで誤導した**エージェントを捉えました —
  ライブチャットでは隠れてしまうもので、あなたを誤った判断へと導きます。
- **使い切ったコンテキストの先へ — 詳細を失わずに。** `/compact` は具体を
  消してしまいます。代わりに新しいセッションを開けば、前のセッションの
  **ログ**を読み、その結論から続けられます — 既に片付いたことにコンテキストを
  再び燃やすことなく。元のセッションは監査と検索のためにそのまま残ります。
  新しいセッションは**どの**エージェントでも走らせられます。履歴はツールに
  かかわらず突き合わせられます。
- **あなたのメモリシステムに供給する。** Karpathy 流のメモリや要約を保つ、
  あるいは自前の手法？ `ai-r` は AI チャットについて、あなたがメッセージ履歴で
  すでに行っていることを与えます — 大切な詳細の永続的なメモリを構築するための、
  パース済みエンティティを。
- **何を、なぜやったかを思い出す。** なぜこのファイルは編集されたのか？ なぜ
  このルールは追加されたのか？ ファイルが変わったセッションを見つけ、編集の
  *前*にある依頼を読みます。

## セッション検索ツールとの違い

いくつかのエージェント横断ツールが、今では複数エージェントの履歴を読みます
（`jazzyalex/agent-sessions`、`Dicklesworthstone/coding_agent_session_search`、
`hacktivist123/agent-session-resume`）。そのほとんどは**検索とタイムライン**に
関するものです。*セッション*を見つけ、履歴をスクロールする。

`ai-r` はより深く踏み込みます。**プラン・意図・作者性を、そのまま使える
エンティティとして**取り出し、あなたはその上にメモリを構築します。検索は
テキストを見つけます — `ai-r` は**なぜ**に答えます。技術的には検索ツールも
セッションのテキストからプランを掘り出せますが、単一の正規化された形に
パースして返しはしません — `ai-r` ではそれこそが主要なサーフェスです。

| 能力 | 単一エージェントビューア | エージェント横断検索ツール | `ai-r` |
|---|---|---|---|
| 2 つ以上のエージェントのログを読む | いいえ | はい | はい — Claude、Codex、OpenCode、Antigravity、Pi |
| プログラム的サーフェス | ほぼ GUI/TUI | ほぼ TUI/CLI/アプリ | **MCP + CLI + Python SDK** |
| 作者特定（編集/コマンド → エージェント + 意図） | — | 部分的 | はい — `find-file-edits` / `find-tool-calls` |
| 監査リプレイ（git なしでセッションの変更を再構成） | — | まれ | はい — `session_diff` |
| プラン抽出（最終 vs ドラフト、正規化済み） | — | — | はい — `plan` |
| スコープ | ビューア | 検索 / 再開 / メモリ | **読み取り専用の抽出コア** |

*競合各列は 2026-07 時点の公開ドキュメントを反映しています。能力が不明確な場合は、過大に主張するのではなく控えめに記載しています。*

私たちはあえて、エージェントの幅・速度・TUI の充実さでは**競いません**。
`ai-r` の切り込みどころは、機械が消費するための「なぜ」と構造化された
エンティティを取り出すことです。

## 実運用での実績

`ai-r` はすでに自身の開発履歴を読んでいます — 5 つのエージェントすべてを
横断して。実際のツールがその上で動きます（それらは別に、読み取り専用 API の
上に存在します）。

- **auditor** — 新鮮なエージェントが、前のエージェントが実際に何を行い決めたかを
  冷静にチェックします。これはプランについてこっそり嘘をついたエージェントを
  捉えました。
- **summarizer**（`export rounds`）— セッションをそのまま引き継ぎに使える
  ドキュメントへレンダリングします。
- **ai-local-reader** — 読み取り専用のスキル。ディスク上の過去セッションを
  全エージェント横断で監査します。

これらのツールはワークフロー側にあり、このリポジトリの外です。`ai-r` 自身は
データを読んで返すだけです。

## 対応エージェント

| エージェント | ストレージ | パーサー |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite（snap/flatpak 自動検出） |
| Antigravity | `~/.gemini/antigravity/brain/` | JSON / markdown の brain ディレクトリ |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

あなたのエージェントがない？ 6 つ目を追加するのは**パーサーモジュール 1 つ**
です。読み取り専用のパターンは、どのツールにも数分で移植できます。
[CONTRIBUTING.md](./CONTRIBUTING.md) を参照してください。

## サーフェス

`ai-r` は同じ読み取り能力を 3 通りで提供します。

- **MCP サーバー**（`ai-r-mcp`）— stdio JSON-RPC 上の 13 ツール。どの MCP
  エージェントも直接呼び出せます（推奨）。登録方法は
  [docs/mcp-registration.md](./docs/mcp-registration.md) を参照。
- **CLI**（`ai-r`）— スクリプトや手動利用のためのサブコマンド（`list` / `read` /
  `search` / `find-file-edits` / `find-tool-calls` / `file-frequency` /
  `detect-agent` / `export rounds`）。検索演算子は
  [docs/search-operators.md](./docs/search-operators.md)。
- **Python SDK**（`from ai_r.parsers import ...`）— パーサー、型付きの
  `Session`/メッセージモデル、そしてイベント動詞。自前のツールを構築するために。

### メソッド語彙（SSOT）

以下のブロックは [`docs/methods.md`](./docs/methods.md) からフレーム化されて
います — 公開動詞とプリセットの英語版信頼できる情報源です。そのファイルの
マーカーブロックと同期されています。

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

**Adaptive output truncation (`output_mode`):** the per-record `output` cap is `_OUTPUT_CHARS_CAP = 2000` chars. How that budget is spent is controlled by `output_mode ∈ {"head", "tail", "smart"}`. The default (`output_mode=None`) is **adaptive per record**: a record with `is_error == True` is truncated `"smart"` (surface the error lines — `error`/`fatal`/`traceback`/… — plus the tail, so an error at the *end* of a long log is not lost to a head-only cut), while a successful record is truncated `"head"` (legacy behaviour). An explicit `output_mode` forces one strategy for every record. `smart`/`tail` may return up to ~2× the cap to keep both the surfaced lines and the tail; whenever `output` is cut it is still named in that record's `truncated_fields`.

**Filtering `find_tool_calls` (all optional, composed by AND):** beyond `tool_name`/`tool_name_pattern`, records can be narrowed by `input_contains` (case-insensitive substring over the serialized tool input / command text), `output_contains` (ci substring over the correlated `output`), `output_excludes` (drop a record whose `output` contains the marker — a caller-supplied noise filter, e.g. a harness security-gate line, `"user rejected"`, `"MANUAL COMMIT BLOCKED"`; **no such list is hard-coded in the core**), and `is_error` (tri-state: `None` = all, `True` = errors only, `False` = successes only). All filters intersect (AND). There is **no** dedicated "error + domain" verb: that pairing is a *composition* — e.g. `find_tool_calls(input_contains="git", is_error=True)` returns the real command failures of a chosen domain (`git` is just an example domain, not a special case).

**`is_error` (tool-call outcome) is cross-agent best-effort:** **Claude** and **OpenCode** carry a real success/error flag (Claude's `tool_result.is_error`; OpenCode's `state.status == "error"`). **Codex** and **Pi** expose no error field on their result records → `is_error` is always `False` (absence of a flag, not a proof of success). **Antigravity** emits no tool-result records at all → no outcome signal. Consumers must not read a cross-agent `is_error=False` as "verified success" for Codex/Pi/Antigravity. `find_tool_calls` now carries the same `is_error` per record, plus the correlated `output` (tool-result content, char-capped) — correlation is by tool_use_id (Claude `tool_use.id` / OpenCode `callID`); with the same best-effort caveat (`is_error` is authoritative only for Claude/OpenCode, and defaults to `False` for Codex/Pi/Antigravity or when no result correlates). To make that honesty machine-readable, each `find_tool_calls` record also carries `is_error_reliable` (bool): `True` for Claude/OpenCode (a real flag backs the value), `False` for Codex/Pi/Antigravity (no source → `is_error` is always `False` and may **undercount** true failures). A consumer filtering `is_error=True` should read `is_error_reliable` to know whether a `False` means "verified success" or merely "no signal".

## Empty results & session lookup

**Empty-result diagnostics (a zero-result response explains itself, never a bare empty list):** when a scanning method — `query`, `search_sessions`, `find_tool_calls`, `find_file_edits`, `list_sessions` — matches nothing, the response carries a `diagnostics` object next to the empty list/count. Shape: `scanned` (one entry per scanned agent — `sessions` count, `date_min`/`date_max`, `source_found`, plus a per-agent `hint` such as `source not found: ~/.pi/agent/sessions` or `source present but contains no sessions`), `corpus` (total sessions + overall date bounds), `filters` (echo of the active filters), `hints` (cause candidates: a `since`/`until` bound that excludes the entire corpus is called out explicitly — e.g. `since='2030-01-01' is after the newest session (…) — the date filter excludes the entire corpus`; otherwise the remaining filters are named, or the result is declared a genuine no-match). Diagnostics are computed only on the empty path — a non-empty response never carries (or pays for) them — and never crash the response (an unreadable source degrades to a per-agent hint).

**`read_session` no longer requires `agent`:** the parameter is optional. When omitted, the id is looked up across every parser (session ids are unique across agents in practice). A rare cross-agent id collision returns `{ambiguous: true, candidates: [...], count}` — a disambiguation list where each candidate carries its `agent`, NOT an error; re-ask with an explicit `agent`. A miss returns `{error: "not_found", agents_scanned: [...]}`. `get_body` was already agent-free (its event id embeds the owning session).

**CLI error contract (a consumer script never sees a Python traceback):** expected failures keep the single `ai-r: <message>` stderr line + non-zero exit (1 generic / 2 ambiguous or invalid / 3 not found); an *unexpected* internal error is emitted as one structured JSON line on stderr (`{"error": "internal_error", "type", "message", "hint"}`) with exit code 1. `AI_R_DEBUG=1` re-raises the original exception for debugging.

<!-- methods:end -->

### イベントコア

上記の動詞は新しいものです。単一の**イベントコア**が、その場しのぎのツールの
山を置き換えます。各パーサーは 1 つのエージェントのログを読み、型付きモデルを
発行し、単一のエージェント中立なストリームへ正規化します — `user_turn` /
`assistant_turn` / `tool_call(...)` / `plan_event`。少数の動詞がそのストリームを
フィルタし、集約し、差分化します。エージェントの違い（`ExitPlanMode` vs
`update_plan` vs `implementation_plan.md`）はパーサーの内部に隠れたまま —
呼び出し側は 1 つの形状を見ます。

正直な境界: これは**エンティティの抽出だけ**です — ターン、ツール呼び出し、
プラン、意図、リアクション。グラフでは**なく**、メモリストアでも**ありません**。
次に何をするか（ナレッジグラフ、Obsidian、永続メモリ）はあなたの側、この
リポジトリの外です。レイヤリング全体と MCP ツール一覧は
[docs/architecture.md](./docs/architecture.md) を参照してください。

## クイックスタート（1 コマンド）

要件: `venv` または `pip` を備えた Python 3.11+、そして `jq`（Claude と
Antigravity の MCP 設定を自動パッチするために使用 — 他は `jq` を必要としません）。

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

インストーラは venv を作成し、ランタイムパッケージをインストールし、
**Claude**、**Codex**、**OpenCode**、**Antigravity** の MCP 設定を（設定が
存在する場所で）パッチし、**Pi** の CLI スキルをインストールし、スモーク
テストを実行します。

## 境界: リーダーであって、ガードではない

- **読み取り専用。** エージェントのコードを実行することも、その履歴に書き込む
  ことも決してありません — 読んで返すだけです。
- **グラフなし、メモリなし。** エンティティ（ターン、呼び出し、プラン、意図）を
  取り出します。それらからナレッジグラフやメモリを構築するのはあなたの仕事で、
  ai-r の仕事ではありません。
- **アクセス制御レイヤーではない。** CLI、MCP サーバー、パッケージに到達できる
  者は誰でも任意のセッションを読めます。パーサーの前に認可はありません。信頼
  できないローカルプロセスが到達できない場所に置いてください。
- **セッションの内容はデータであり、コマンドではない。** 読み手（auditor、
  summarizer）はセッションのテキストを命令ではなくデータとして扱わなければ
  なりません。[Security](docs/security.md) を参照してください。

<!-- scenarios:start -->

## Acceptance summary

Full spec: [docs/scenarios.md](docs/scenarios.md) — 45 LLM-executed end-to-end scenarios validating the whole public surface on a real vault. Kept in English as language-neutral, executable test specs.

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
| `find_tool_calls` | 4 | Exact `tool_name` vs substring `tool_name_pattern` search, cross-agent; neither/both arguments **fail loud** (`invalid_argument`), never a silent empty result; each record surfaces the correlated `is_error` outcome + char-capped `output` (authoritative for Claude/OpenCode, best-effort elsewhere) + `is_error_reliable`; `input_contains`/`output_contains`/`output_excludes`/`is_error` filters compose by AND (domain × error without a special verb); adaptive `output_mode` (`smart` for errors) keeps a trailing error line that `head` would drop. |
| `read_session` | 3 | Reads one session into the compact `{role, content}` projection with metadata + pagination echo; `offset`/`limit` page a stable ordered list, `total` invariant across slices; `agent` is **optional** — an id resolves across every parser, a rare cross-agent id collision returns a `candidates` list (not an error), a miss names `agents_scanned`. |
| `search_sessions` | 3 | Title/body/all scope; `AND` default, `OR` widens (`AND ⊆ OR`), negative `-term` excludes, quoted phrase is contiguous; `scope=body` returns a matching `snippet`; BM25 vs date sort. |
| empty-result diagnostics (cross-cutting) | 2 | A zero-result `query`/`search_sessions`/`find_tool_calls`/`find_file_edits`/`list_sessions` response carries `diagnostics` (per-agent scan counts + date bounds + `source_found`, corpus totals, cause hints: missing source dir / all-excluding `since`/`until` / remaining filters); a non-empty response never carries it. |
| CLI error contract | 1 | A failing `ai-r` CLI invocation exits non-zero with a structured error on stderr (single `ai-r: …` line, or one JSON `internal_error` line for unexpected failures) — never a Python traceback; `AI_R_DEBUG=1` re-raises for debugging. |

<!-- scenarios:end -->

## 次に — ドキュメント

- メソッド語彙（動詞 + プリセット）— [`docs/methods.md`](./docs/methods.md)
  （英語 SSOT）· [`docs/methods.ru.md`](./docs/methods.ru.md)（ロシア語ミラー）
- 受け入れシナリオ（32 個の e2e）— [`docs/scenarios.md`](./docs/scenarios.md)
- アーキテクチャとレイヤリング — [`docs/architecture.md`](./docs/architecture.md)
- 検索演算子 — [`docs/search-operators.md`](./docs/search-operators.md)
- エージェントごとの MCP 登録 — [`docs/mcp-registration.md`](./docs/mcp-registration.md)
- パーサーの対応範囲と制限 — [`docs/parsers.md`](./docs/parsers.md)
- セキュリティ（信頼できない内容）— [`docs/security.md`](./docs/security.md)
- 6 つ目のエージェントを追加 — [`CONTRIBUTING.md`](./CONTRIBUTING.md)

## 開発

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ 個のテスト、CI はカバレッジ ≥80% を要求
- Conventional Commits（`feat:`、`fix:`、`docs:`、…）
- 新しいエージェントの追加時は [CONTRIBUTING.md](./CONTRIBUTING.md) と
  [docs/parsers.md](./docs/parsers.md) を参照

<details>
<summary>Keywords</summary>

claude code session reader · claude code session parser · codex session parser ·
opencode session reader · antigravity brain parser · pi agent session reader ·
cross-agent attribution · ai coding agent audit · ai agent session history ·
mcp session tools · read-only session reader · agent session replay ·
resume agent session · agent handoff · plan extraction · tool-call audit ·
file edit attribution · multi-agent coding · claude codex opencode antigravity pi

</details>

## ライセンス

MIT — [LICENSE](./LICENSE) を参照してください。

---

**始めるには:** clone して `bash install.sh`、次にあなたのエージェント用に
MCP サーバーを登録し（[docs/mcp-registration.md](./docs/mcp-registration.md)）、
ホストツールを再起動します。すべてのエージェントの履歴への、単一の読み取り
専用サーフェスです。
