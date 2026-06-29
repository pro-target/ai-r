# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> **すべての AI エージェントのセッション履歴を、読み取り専用の単一窓口で** ——
> Claude、Codex、OpenCode、Antigravity、Pi を、MCP・CLI・Python
> パッケージのいずれかで。
>
> コンテキストを失わずにエージェントを切り替える · どのエージェントが何をした
> *か、何を実行したか* を監査する · すべてのセッションを一度に検索する。

```bash
# 1 クエリで全エージェントを横断 —— あの認証バグが出たセッションを探す
ai-r search "auth token refresh" --scope body
```

## なぜ?

AI コーディングエージェントはどれも、会話履歴をそれぞれ独自の場所・独自の形式で
保持します。Claude と Codex は JSONL を書き、OpenCode は SQLite を使い、
Antigravity は「brain」ディレクトリに散らばらせ、Pi はプロジェクト単位の JSONL を
書きます。つまりあなたの作業は**ツールごとにサイロ化**されている —— エージェントを
切り替えると文脈が途切れ、「別のエージェントはもう何を試したか?」を尋ねることが
できません。

`ai-r` はそのすべてを**単一の読み取り専用インターフェース**にまとめます。どの
エージェントでも —— スクリプトでも、あなた自身でも —— どのツールが書いたかに関わ
らず、任意のセッションを指して読めます。あなたが動かすすべてのエージェントを横断
する共有メモリです。

## 実証 —— 自分の作業で実際に使っています

`ai-r` は、その `ai-r` 自身を作ったまさにそのセッションを読みます。**5 つの
エージェント**と**684 件の記録済みセッション**にわたって、これまでに**約 125 回**
呼び出されました:セッション読み取り 49 回、本文検索 37 回、一覧 31 回、ファイル
編集の追跡 9 回。最も多い用途は**監査** —— 前のエージェントが実際に何をして、何を
決めたのかを冷静にレビューすることだけを任務とする、まっさらなエージェントです。
これにより、計画(と私)をひそかに誤導していたエージェントを捕まえられました。
今ではそうした取りこぼしも検知できます。

## 便利な場面

すべてのエージェントを横断する単一のリーダーは、単一エージェントのログでは
不可能なワークフローを実現します:

- **プロバイダーの上限に達した?エージェントを切り替えて続行。** タスク途中で
  Codex のクォータを使い切った?Antigravity を立ち上げて Codex セッションを
  指し、「続きをやって」と頼む —— 同じタスク、別モデル、コンテキストの喪失
  なし。
- **コンテキストウィンドウを使い切った?新規で開始して再開。** 新しいセッション
  を開き、前のセッションの UUID を渡して「ここから続けて」と言います。前の
  トランスクリプトは、どのエージェントが書いたかに関わらず読めます。
- **エージェント間の引き継ぎとトリアージ。** 「別のエージェントはこれについて
  何をした?」が、Claude、Codex、OpenCode、Antigravity、Pi 間で通用します。
  5 種類のログ配置を学ぶ必要はありません。
- **「誰がこのファイルを、いつ触った?」** あるパスへのすべての編集 —— すべての
  エージェント、すべてのセッションにわたり —— タイムスタンプ付き。期間を区切っ
  た監査:「先週エージェントは `src/auth.py` に何をした?」(`find-file-edits`
  参照)。
- **エージェントが*実行した*ことを監査 —— 変更内容だけでなく。** すべてのツール
  呼び出し —— シェルコマンド、ファイル書き込み、Web 取得、MCP 呼び出し —— を
  全エージェント横断で、それぞれを引き金となったユーザーの要求にひも付けて。
  「先週どれかのエージェントがデプロイを実行した?」「Codex が実行した全シェル
  コマンドを見せて」(`find-tool-calls` 参照)。
- **セッションを CHANGELOG ラウンドとして描画。** セッションを 目標 / 状態 /
  触ったファイル / 決定 / 次のアクション の markdown に描画 —— 別エージェント
  や朝会に貼れる引き継ぎドキュメント(`export rounds` 参照)。
- **「自分はどのエージェントで、どのセッションにいる?」** 新規に立ち上がった
  スクリプトやエージェントが自身のセッション UUID を検出し、自分自身または
  前任者を読んでプログラム的に再開します(`detect-agent`、`detect-session`
  参照)。
- **クラッシュからの復旧。** エージェントが落ちた、ターミナルが閉じた、マシンが
  再起動した?自分がいたセッションを検出し、読み戻して、止まったまさにその地点
  から再開できます —— 何も失われていません(`detect-session` 参照)。
- **タイトルだけでなく本文も検索。** 「`auth token` を話題にしたセッションを
  すべて見つける」 —— 全 5 エージェントにわたり、スニペット付き —— `operator`
  モード(`AND`/`OR`/`NOT`)の本文スコープ検索で。「これ前に解決したことが
  ある?」にも最適 —— 過去のセッションを見つけて、やり直す代わりにその修正を
  再利用しましょう。

## クイックスタート(1 コマンド)

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

## アーキテクチャ

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

## 単なるツールではなく、再利用できるコアでもあります

`ai-r` は借用されることを前提に作られています。パーサー、型付きの `Session` と
メッセージモデル、そしてセキュリティヘルパー(信頼できないコンテンツの扱い、
サイズ上限、引用符を考慮したシェル走査)は、小さく、依存が少なく、設計として
読み取り専用です。あなたのエージェントが上の一覧にない?カーネルを取り出し、新しい
ログ形式に向けるだけで、そのためのリーダーが手に入ります —— たいていのエージェント
はパーサーモジュール 1 つで対応できます。リポジトリ全体が「すべてのエージェントの
履歴を、安全に読む」ための実働テンプレートにもなっています。

## 設計の境界

`ai-r` は公開コアです:パーサー、型付きメッセージ、CLI、MCP。ワークフロー固有
のレビューアー、サマライザー、監査器は本リポジトリの外にあり、パーサー API
(`read_messages`)を消費します。

`ai-r` は**リーダーであり、ガードではありません。** CLI、MCP サーバー、
パッケージのいずれかに到達できる呼び出し元は、任意のセッションを読めます ——
パーサーの前にアクセス制御層はありません。信頼できないローカル呼び出し元が
到達できない場所に置いてください。

セッションの内容は**信頼できない** —— リーダーの呼び出し元(監査器、サマライ
ザー、再生エージェント)はそれをデータとして扱い、指示としては扱ってはいけませ
ん。[セキュリティ —— 信頼できないセッション内容](docs/security.md)参照。

## 既知の制限

- **Antigravity** —— フィクスチャ網羅に加え、ローカルに brain ディレクトリが
  ある場合は任意の実データスモークテスト。
- **Codex CLI のシェル編集** —— `find_file_edits` は、引用符を考慮した保守的な
  リダイレクト走査(`>` / `>>`)により、`exec_command` / `local_shell_call`
  シェルコマンドから codex のファイル書き込みを復元します。`tee` / `sed -i` /
  `cp` / `mv` / heredoc のみによる書き込みは検出されません。構造化編集
  (`apply_patch` / `write_file`)は常に検出されます。

パーサーの網羅マトリクス全体は [docs/parsers.md](docs/parsers.md) 参照。

## 使い方

### MCP サーバーとして(推奨)

MCP サーバーはエージェントの設定に自動登録されます。利用可能なツール:

| ツール | 目的 |
|---|---|
| `list_sessions(agent?, limit?, offset?)` | 発見可能なセッションを一覧、任意でエージェント絞り込み。ページネーション:`limit=0` = 上限なし。レスポンスに `total`/`offset`/`limit`/`truncated` を含む。 |
| `read_session(uuid, agent, offset?, limit?)` | 1 セッションを読む。デフォルトで最大 100 メッセージ。`offset`/`limit` でさらにページ送り。 |
| `find_file_edits(path, agent?, since?, until?, limit?)` | 指定パスのセッション横断のすべてのファイル編集を検索。デフォルトで全エージェント横断、任意で期間(`since`/`until` ISO 8601)指定。 |
| `find_tool_calls(tool_name?, tool_name_pattern?, agent?, since?, until?, limit?)` | すべてのツール呼び出し —— シェルコマンド、ファイル書き込み、Web 取得、MCP 呼び出し —— をセッション横断で検索。ツール名を完全一致(`tool_name`)または部分文字列(`tool_name_pattern`)でマッチ。全エージェント横断、期間指定可。各ヒットには引き金となったユーザーの要求が `intent` として付きます。 |
| `search_sessions(query, agent?, scope?, operator?, limit?)` | タイトルやメッセージ本文で検索、`operator` モード(`AND`/`OR`/`NOT`)と Google 風の `-term` 除外付き。[検索演算子](#検索演算子)参照。 |

**ページネーション**(`limit`/`offset`、残りページがあるときの `truncated`
フラグ付き)は MCP ツールと Python SDK の両方に公開 —— [architecture.md](docs/architecture.md)
参照。

### CLI として(テスト / スクリプト)

```bash
# list / read / search
ai-r list --agent pi
ai-r read --agent pi <session-uuid>
ai-r search "refactor"
ai-r search "pwa manifest" --scope body --operator and --agent claude

# 誰がファイルを編集したか、全エージェント横断、任意で期間指定
ai-r find-file-edits src/auth.py --since 2026-06-01 --until 2026-06-30
ai-r find-file-edits "config" --agent claude --limit 20

# エージェントは何を実行したか。ツール名の完全一致または部分文字列パターン、期間指定
ai-r find-tool-calls Bash --since 2026-06-01
ai-r find-tool-calls --pattern deploy --agent codex

# 自分はどのエージェント / どのセッションか(スクリプト、オーケストレーション、自己再開)
ai-r detect-agent --quiet          # → 例 "claude"
ai-r detect-session --json         # → 候補セッション UUID

# セッションを CHANGELOG ラウンドとして描画(引き継ぎドキュメント / 再生)
ai-r export rounds <session-uuid> --include-round --output round.md
```

ほとんどのサブコマンドに `--json` を追加すると機械可読出力になります。

### 検索演算子

`search_sessions`(MCP)と `ai-r search`(CLI)は同じクエリパーサーと同じ
operator パラメーターを共有します。デフォルトの振る舞い(`scope="title"`、
`operator="AND"`、`limit=50`)は、以前のタイトルのみの部分文字列検索と同じです。

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
表に表示)が含まれます —— 最初の一致箇所、最大 200 文字。

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
# タイトルのみ(legacy、デフォルトのまま)
ai-r search "refactor"

# 本文検索、全語必須、claude を除外
ai-r search "pwa manifest -claude" --scope body --operator and

# 本文検索、いずれかの語、最大 5 件
ai-r search "pwa manifest" --scope body --operator or --limit 5

# これらの語のいずれも含まないすべて
ai-r search "auth login" --scope body --operator not
```

### Python SDK として

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
  [設計の境界](#設計の境界)参照。

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

## ライセンス

MIT —— [LICENSE](./LICENSE) 参照。
