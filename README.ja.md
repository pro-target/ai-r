# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> **すべての AI コーディングエージェントのセッション履歴を、読み取り専用の単一窓口で** ——
> Claude、Codex、OpenCode、Antigravity、Pi を、**MCP**・**CLI**・
> **Python SDK** のいずれかで。
>
> コンテキストを失わずにエージェントを切り替える · どの編集やコマンドも
> それを実行したエージェントにひも付ける · セッションを再生する · 作業の
> 背後にある計画を抽出する —— 5 つのエージェントすべてを、単一の
> インターフェースで。

```bash
# one query, every agent — find the session where that auth bug came up
ai-r search "auth token refresh" --scope body
```

## 痛み:5 つのサイロ、共有ビューなし

AI コーディングエージェントはどれも、会話履歴をそれぞれ独自の場所・独自の
形式で保持します:

- **Claude** と **Codex** は JSONL を書き、
- **OpenCode** は SQLite DB を使い、
- **Antigravity** は「brain」ディレクトリに散らばらせ、
- **Pi** はプロジェクト単位の JSONL を書きます。

5 つの形式、5 つの配置。だから複数のエージェントを動かした瞬間、あなたの作業は
**ツールごとにサイロ化**されます。エージェントを切り替えると文脈が途切れます。
「*別の*エージェントはもう何を試したのか?」を尋ねることができません。そして
コミットやファイル編集が現れても、**実際にどのエージェントがやったのか**に
まっすぐ答えられません —— その帰属情報は、一つずつ学ばなければならない 5 つの
互換性のないログの中にあります。

## 約束

`ai-r` はその 5 つすべてを**単一の読み取り専用インターフェース**にまとめます。
どのツールが書いたかに関わらず、任意のエージェント —— またはスクリプト、あなた
自身 —— を任意のセッションに向けてください。すべてのエージェントで同じクエリの
形。形式ごとの違いはパーサーの内部で正規化され、消え去ります。

## 仕組み

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

各パーサーは 1 つのエージェントのディスク上のログを読み、型付きの `Session` と
メッセージモデルを出力します。それらは単一のエージェント中立な**イベント
ストリーム** —— `user_turn` / `assistant_turn` / `tool_call(...)` /
`plan_event` —— に正規化され、小さな **verbs** のセットがそのストリームを
フィルタ・集約・差分します。エージェント間の違い(`ExitPlanMode` vs
`update_plan` vs `implementation_plan.md`)はパーサーの内部に隠され、呼び出し元
には単一の形が見えます。

## 実証 —— 自分を作ったセッションを読む

`ai-r` は、その `ai-r` 自身を作ったまさにそのセッションを読みます。**5 つの
エージェント**にわたり、パーサー API の上で暮らす実際のコンシューマーから日常的に
呼び出されています:

- **session-summarizer** / `export rounds` —— セッションを CHANGELOG 風の
  引き継ぎドキュメントに描画。
- **git-log-auditor** —— 前のエージェントが実際に何をして何を決めたのかを冷静に
  レビューすることだけを任務とする、まっさらなエージェント。これにより、計画を
  ひそかに誤導していたエージェントを捕まえられました。
- **ai-local-reader** —— 5 つのエージェントすべてにわたり、ローカルディスクの
  過去セッションを監査する読み取り専用スキル。
- **MCP 登録** —— サーバーは Claude、Codex、OpenCode、Antigravity に自動登録され、
  Pi には CLI スキルが付きます。

これらのコンシューマーは**ワークフロー側**にあり、本リポジトリの外で暮らします。
彼らは `ai-r` の読み取り専用パーサー API(`read_messages`、MCP ツール、verbs)を
呼びます。`ai-r` 自身はリーダーであり続けます。

## クイックスタート(1 リクエスト)

前提:Python 3.11+ で `venv`(`python3-venv`)または `pip`
(`python3-pip`/`pip3`)、そして `jq`(Claude と Antigravity の MCP 設定の
自動登録に使用 —— 他は `jq` 不要)。

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

以上です。インストーラーは:
- デフォルトでユーザーごと(per-user)モード。`opt` モードは明示指定。
- venv を作り、ランタイムパッケージをインストール。
- その設定ファイルが存在する場合、**Claude**、**Codex**、**OpenCode**、
  **Antigravity** の MCP 設定をパッチ。
- 存在しない場合、**Pi** の CLI スキルを `~/.agents/skills/ai-r/SKILL.md` に配置。
- スモークテストを実行。

## 対応エージェント

| エージェント | ストレージ | パーサー |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite(snap/flatpak を自動検出) |
| Antigravity | `~/.gemini/antigravity/brain/` | JSON / markdown brain ディレクトリ |
| Pi | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` | JSONL |

あなたのエージェントが未対応?6 つ目の追加は**パーサーモジュール 1 つ**で済みます。
読み取り専用のパターンは、どんなツール(Cursor、Cline、自作のもの)にも数分で
移植できます。[CONTRIBUTING.md](./CONTRIBUTING.md) 参照。

## 窓口(Surfaces)

`ai-r` は同じ読み取り能力を 3 つの方法で公開します:

- **MCP サーバー**(`ai-r-mcp`)—— stdio JSON-RPC 上の 13 ツール。MCP 対応の
  エージェントなら直接呼べます(推奨)。
- **CLI**(`ai-r`)—— スクリプトや手動利用のためのサブコマンド。
- **Python SDK**(`from ai_r.parsers import ...`)—— パーサー、型付きの
  `Session`/メッセージモデル、イベント verbs。自作ツールの構築用。

### メソッド語彙(SSOT)

以下のブロックは [`docs/methods.md`](./docs/methods.md) —— 公開 verbs と
プリセットの単一の真実の源 —— からフレームされています。そのファイルのマーカー
ブロックと同期して保たれます。

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

### このブランチが追加するもの —— イベントコア

上記の verbs は新しいものです:単一の**イベントストリームコア**が、使い捨ての
ツールの山を置き換えます。ハイライト:

- **`query`** —— 主力。統一イベントストリームを `type` / `agent` / `session` /
  日付 / `file` / `tool` / `text` でフィルタ。`sort="relevance"` ではテキスト
  一致が BM25 でランク付け(`search_sessions` と同じスコアラー)。
  `relative_to`+`direction`+`n` では隣接するターンを歩きます —— `intent` と
  `reaction` の両方の背後にあるプリミティブ。
- **`intent` / `reaction` プリセット** —— `intent(event)` = イベントの*背後*にある
  ユーザーの要求(遡る);`reaction(event)` = アシスタントのターンの*後*の
  ユーザーの応答(前進 —— 批評、修正、承認)。
- **`plan`** —— セッションごとに正規化された plan atom、タスク単位でグループ化し、
  `final` vs `draft` vs `completed_major` とタグ付け。だから、計画信号が異なる
  Claude、Codex、Antigravity にわたって、*エージェントが落ち着いた計画*と破棄
  された改訂を抽出できます。`get_body(..., shallow=True)` はサブエージェントに
  最終計画だけを渡し、下書きは省きます。
- **`aggregate` / `diff` / `detect_current`** —— 汎用のロールアップ、ファイル
  単位の縫合済み差分、ランタイムの自己識別。`session_stats` と `session_diff` は
  今やこれらの薄いプリセットで、バイト単位で同一の出力が実データで実証されて
  います(上の SSOT ブロック参照)。

正直なスコープ:これは**読み取り専用のエンティティ抽出**です —— ターン、ツール
呼び出し、計画、intent、reaction。グラフでもメモリストアでも**ありません**。
コンシューマーが次に何をするか(ナレッジグラフ、Obsidian、永続メモリへ分割)は
意図的に**スコープ外**で、コンシューマー側にあります。

### MCP ツール

MCP サーバーは 13 のツールを公開します。読み取りの必須ツール:

| ツール | 目的 |
|---|---|
| `list_sessions(agent?, limit?, offset?)` | 発見可能なセッションを一覧、任意でエージェント絞り込み。ページネーション対応。 |
| `read_session(uuid, agent, offset?, limit?)` | 1 セッションを読む。デフォルトで最大 100 メッセージ、`offset`/`limit` でページ送り。 |
| `find_file_edits(path, agent?, since?, until?, limit?)` | 指定パスのすべてのファイル編集。デフォルトで全エージェント横断、任意で期間指定。 |
| `find_tool_calls(tool_name?, tool_name_pattern?, agent?, since?, until?, limit?)` | すべてのツール呼び出し —— シェル、ファイル書き込み、Web 取得、MCP 呼び出し —— それぞれが引き金となったユーザーの要求を `intent` として運びます。 |
| `search_sessions(query, agent?, scope?, operator?, limit?, sort?)` | タイトルや本文を `AND`/`OR`/`NOT` と Google 風の `-term` で検索;`sort=relevance`(BM25)または `date`。 |
| `session_stats(agent?, since?, until?, group_by?, top?)` | セッションを `agent`/`dir`/`date`/`kind` でグループ化 + ランク付け。 |
| `session_diff(session_uuid, agent, path?)` | git なしで、セッションが変更した内容をファイル単位で再構築。 |
| `query`, `plan`, `get_body`, `aggregate`, `diff`, `detect_current` | 上記のイベントコア verbs。 |

**ページネーション**(`limit`/`offset`、残りページがあるときの `truncated`
フラグ付き)は MCP ツールと Python SDK に公開 —— [architecture.md](docs/architecture.md)
参照。

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

ほとんどのサブコマンドに `--json` を追加すると機械可読出力になります。
イベントコア verbs(`query`/`plan`/`aggregate`/`diff`/`detect_current`)は MCP と
Python SDK で利用できます。CLI は上記のサブコマンドをカバーします。

#### 検索演算子

`search_sessions`(MCP)と `ai-r search`(CLI)は同じクエリパーサーと同じ
operator パラメーターを共有します。デフォルトの振る舞い(`scope="title"`、
`operator="AND"`、`limit=50`)は、以前のタイトルのみの部分文字列検索です。

**クエリ構文**

| 形式 | 例 | 意味 |
|---|---|---|
| 裸の単語 | `pwa manifest` | 両方の語(operator が組み合わせ方を決定)。 |
| 引用符付きフレーズ | `"exact phrase"` | 単一のリテラル語。 |
| 否定プレフィックス | `-claude` | Google 風:この語は現れてはいけない。 |

クエリ内の `AND`、`OR`、`NOT` はリテラルの検索語です。ブールの振る舞いは
`--operator and|or|not`(CLI)または `operator="AND"|"OR"|"NOT"`(MCP)で選択
します。

**operator モード**(正の語をどう組み合わせるか)

| モード | `pwa manifest` の意味 | `pwa -claude` の意味 |
|---|---|---|
| `AND`(デフォルト) | 両方が現れる | `pwa` が現れ、`claude` は現れない |
| `OR` | 少なくとも一方が現れる | いずれかの `pwa` が現れ、`claude` は現れない |
| `NOT` | どちらも現れない | `pwa` も `claude` も現れない |

**scope モード**

| Scope | 検索場所 |
|---|---|
| `title`(デフォルト) | `session.title` のみ —— 履歴のタイトルのみ挙動と一致。 |
| `body` | 各セッションのメッセージテキスト + `tool_use[*].input` + `tool_result[*].content`。 |
| `all` | タイトルまたは本文。 |

`scope` が `body` か `all` で一致すると、結果に `snippet` フィールド(CLI では
表に表示)が含まれます —— 最初の一致箇所、最大 200 文字。結果はデフォルトで
BM25 ランク付け(`sort=relevance`);`sort=date` を渡すと新しい順に並びます。

**パフォーマンスの注意**:`body` と `all` は候補セッションごとに
`read_messages` を呼び出します。大きな保管庫では初回実行が遅くなる可能性が
あります。反復中は `--limit` を上げて結果セットを抑えてください。

**MCP の例**

```python
search_sessions(
    query='pwa -claude',
    agent='claude',
    scope='body',
    operator='AND',
    limit=20,
)
```

**CLI の例**

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

完全な階層化は [docs/architecture.md](./docs/architecture.md) 参照。

## ユースケース —— 実際のコンシューマーごとに 1 つの仕事

すべてのエージェントを横断する単一のリーダーは、単一エージェントのログでは
不可能な仕事を解き放ちます:

- **エージェント間の帰属 —— 「どのエージェントがこれをやった?」** あるパスへの
  すべての編集、すべてのツール呼び出しを、すべてのエージェントとセッションに
  わたり、引き金となった要求とともにタグ付け。期間を区切って:「先週エージェント
  は `src/auth.py` に何をした?」—— `find-file-edits` / `find-tool-calls`。
  **git-log-auditor** の動力源。
- **監査と再生 —— エージェントが実際に何をしたかを冷静にレビュー。** まっさらな
  エージェントが前のセッションを読み、主張した内容ではなく*実行した*内容を
  報告します。`session_diff` は git なしでファイル単位の変更を再構築し、
  `export rounds` は CHANGELOG 風の引き継ぎを描画します。**session-summarizer**
  と **ai-local-reader** の動力源。
- **再開と引き継ぎ —— タスク途中でエージェントを切り替え、文脈を保つ。**
  プロバイダーの上限に達した、あるいはコンテキストウィンドウを使い切った?
  新しいセッション(どのエージェントでも)を開始し、前のセッションの UUID を
  渡して続行。前のトランスクリプトは、どのツールが書いたかに関わらず読めます ——
  `read_session`、`detect-session`。
- **ファイル編集 + intent の発見 —— なぜこのファイルは変わり続けたのか?**
  `file-frequency` は、編集数・別個のセッション・別個の要求・関与エージェントで
  ランク付けして、どのファイルが最も頻繁に変わるかをロールアップします。各編集は
  その背後にあるユーザーの要求を `intent` として運びます。
- **計画の抽出 —— エージェントが落ち着いた計画を復元。** `plan` はタスク単位で
  正規化された plan atom を返します。`final` 対 `draft`、Claude / Codex /
  Antigravity にわたって。`get_body(..., shallow=True)` でサブエージェントに
  最終計画だけを渡せます。

## 代替手段との差別化

*WebSearch で検証、2026-07-01。* 単一エージェントのビューアー領域は混雑して
います(claude-code-viewer、claude-code-history-viewer、claude-session-viewer、
simonw/claude-code-transcripts、claude-view);少数の新しいツールは*実際に*
エージェント横断です(jazzyalex/agent-sessions、
Dicklesworthstone/coding_agent_session_search、hacktivist123/
agent-session-resume)。`ai-r` が異なる点:

| 能力 | 単一エージェントのビューアー | エージェント横断のセッションツール | `ai-r` |
|---|---|---|---|
| 複数エージェントのログを読む | いいえ | はい | はい —— Claude、Codex、OpenCode、Antigravity、Pi |
| プログラム的な窓口 | ほぼ GUI/TUI | ほぼ TUI/CLI/アプリ | **MCP + CLI + Python SDK** |
| 帰属(編集/コマンド → エージェント + intent) | —— | 部分的(一部は provenance あり) | はい —— `find-file-edits` / `find-tool-calls`、各々に `intent` |
| 監査再生(セッションが変更した内容を git なしで再構築) | —— | まれ | はい —— `session_diff` |
| 計画の抽出(final vs draft、正規化済み) | —— | —— | はい —— `plan` |
| スコープ | ビューアー | 検索 / 再開 / メモリ | **読み取り専用の抽出コア**(グラフ/メモリはコンシューマーに委ねる) |

一部のエージェント横断ツールは*逆*方向 —— 永続メモリや協調レイヤー(例:
`cass_memory_system`、`mcp_agent_mail`)—— へ進みます。`ai-r` は意図的に読み取り
専用の抽出で止まります:メモリとグラフはコンシューマー側であり、組み込まれて
いません。競合の正確な能力が公開ドキュメントから不明な場合、上の表は過大主張
ではなく控えめに述べています。

## 設計の境界 —— リーダーであり、ガードではない

- **読み取り専用。** `ai-r` はエージェントのコードを実行せず、エージェントの
  セッションストレージに書き込みません。読んで返すだけです。
- **グラフなし、メモリなし。** エンティティ(ターン、ツール呼び出し、計画、
  intent)を抽出します。その上にナレッジグラフや永続メモリを構築するのは
  コンシューマーの仕事であり、本リポジトリのスコープ外です。
- **アクセス制御層ではない。** CLI、MCP サーバー、パッケージのいずれかに到達
  できる呼び出し元は、任意のセッションを読めます —— パーサーの前に認可は
  ありません。信頼できないローカル呼び出し元が到達できない場所に置いてください。
- **セッションの内容は信頼できない。** リーダーの呼び出し元(監査器、
  サマライザー、再生エージェント)は、セッション内容を*指示ではなくデータ*として
  扱わなければなりません。[セキュリティ —— 信頼できないセッション内容](docs/security.md)参照。

ワークフロー固有のレビューアー、サマリー、監査は本リポジトリの外にあり、
パーサー API(`read_messages`)を消費します。

### 既知の制限

- **Antigravity** —— フィクスチャ網羅に加え、ローカルに brain ディレクトリが
  ある場合は任意の実データスモークテスト。
- **Codex CLI のシェル編集** —— `find_file_edits` は、引用符を考慮した保守的な
  リダイレクト走査(`>` / `>>`)により、`exec_command` / `local_shell_call`
  シェルコマンドから codex のファイル書き込みを復元します。`tee` / `sed -i` /
  `cp` / `mv` / heredoc のみによる書き込みは検出されません。構造化編集
  (`apply_patch` / `write_file`)は常に検出されます。

パーサーの網羅マトリクス全体は [docs/parsers.md](docs/parsers.md) 参照。

## MCP 登録

`ai-r-mcp` は stdio MCP サーバーです。ホストツールごとに 1 回登録します。
`USER` をユーザー名に置き換えてください(`ai-r-mcp` が `PATH` にあれば絶対
パスは省略可能)。**設定編集後はホストツールを再起動してください** ——
どれも MCP の変更をライブで取り込みません。

下のスニペットは `/home/USER/.local/bin/ai-r-mcp` を使います。別の場所に
インストールしている場合は調整してください(`which ai-r-mcp` で確認)。

### Claude Code

`~/.claude.json`(トップレベルの `mcpServers` オブジェクト)を編集:

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

単一プロジェクトの登録にするには、リポジトリルートに `.mcp.json` をコミット
してください([`.mcp.json`](./.mcp.json) 参照)。

### Codex

`~/.codex/config.toml` を編集:

```toml
[mcp_servers.ai-r]
command = "/home/USER/.local/bin/ai-r-mcp"
args = []
```

### Gemini CLI

`~/.gemini/settings.json`(`mcpServers` オブジェクト)を編集:

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

`~/.config/opencode/opencode.jsonc`(トップレベルの `mcp` オブジェクト)を編集。
OpenCode は他と 3 点異なります:`type` が `"local"`(`"stdio"` でない)、
`command` が単一の統合された配列(コマンド + 引数を一緒に)、環境キーが
`"environment"`。

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

`~/.gemini/antigravity/mcp_config.json`(`mcpServers` オブジェクト)を編集。
上の Gemini CLI 設定とは別物です —— Antigravity は MCP 設定を
`~/.gemini/antigravity/` に置きます。

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

### Pi —— MCP ではなくスキル

Pi(`@earendil-works/pi-coding-agent`)には**編集すべき MCP サーバー設定が
ありません**。`mcpServers` マップではなく、拡張/スキルモデル
(`pi install <source>`、`pi config`)を使うため、`ai-r-mcp` を Pi のプロセス
内 MCP ツールとして登録できません(プロセス内で起動すると Pi の設計契約に違反
します)。代わりに `install/agent-configs.sh` が読み取り専用の **CLI スキル**
を `~/.agents/skills/ai-r/` —— Pi が既にスキャンしているディレクトリ —— に
配置します。このスキルはモデルに Pi の bash セッションから `ai-r` CLI を呼ばせ
ます(MCP 起動なし)。Pi セッションは `ai-r` から CLI
(`ai-r list --agent pi`、`ai-r read …`)や Python SDK でも完全に読めます。
どちらも `~/.pi/agent/sessions/` ファイルを直接読みます。`/ai-r` スラッシュ
コマンドを使うには `~/.pi/agent/settings.json` で `enableSkillCommands: true`
を設定してください(デフォルト `false` でもスキルのテキストは機能します)。

### 注意

- `ai-r-mcp` は `PATH` になければなりません。さもなくば上記の絶対パスを使ってください。
- JSON 設定のパッチは `jq` を使います。`jq` がない場合、Codex、OpenCode、Pi
  の登録は完了しますが、Claude と Antigravity の設定はスキップされます ——
  `jq` を入れるか、上記のスニペットで手動登録してください。
- 設定ファイル編集後にホストツールを再起動してください。
- サーバーは読み取り専用。到達できる呼び出し元は任意のセッションを読めます。
  [設計の境界](#設計の境界--リーダーでありガードではない)参照。

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

## 開発

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350 以上のテスト、CI はカバレッジ ≥80% を要求。
- Conventional Commits(`feat:`、`fix:`、`docs:`、…)。
- 新しいエージェントの追加は [CONTRIBUTING.md](./CONTRIBUTING.md) と
  [docs/parsers.md](./docs/parsers.md) 参照。
- `src/ai_r/validators/` と `src/ai_r/templates/` はオプションのスタンドアロン
  ヘルパー(session-note markdown の検証)で、CLI や MCP の表面には含まれません。

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

MIT —— [LICENSE](./LICENSE) 参照。

---

**始めるには:** clone + `bash install.sh`、そして自分のエージェント用に MCP
サーバーを登録([Claude](#claude-code) · [Codex](#codex) ·
[OpenCode](#opencode) · [Antigravity](#antigravity) · [Pi](#pi--mcp-ではなくスキル))
してホストツールを再起動。すべてのエージェントの履歴を、読み取り専用の単一
窓口で。
