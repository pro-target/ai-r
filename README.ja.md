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
- **MCP 経由で動作（15 ツール）。** エージェントが `ai-r` を平易な言葉で直接
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

- **MCP サーバー**（`ai-r-mcp`）— JSON-RPC 上の 15 ツール。どの MCP
  エージェントも直接呼び出せます（推奨）。既定は **stdio**。オプションで
  **共有 http サーバー**（エージェントごとに stdio を乱立させる代わりに、
  すべてのエージェント向けにウォームなプロセスを 1 つ）も使えます。
  クイックスタートの `http` エクストラを参照。登録方法は
  [docs/mcp-registration.md](./docs/mcp-registration.md) を参照。
- **CLI**（`ai-r`）— スクリプトや手動利用のためのサブコマンド（`list` / `read` /
  `search` / `find-file-edits` / `find-tool-calls` / `file-frequency` /
  `detect-agent` / `export rounds`）。検索演算子は
  [docs/search-operators.md](./docs/search-operators.md)。
- **Python SDK**（`from ai_r.parsers import ...`）— パーサー、型付きの
  `Session`/メッセージモデル、そしてイベント動詞。自前のツールを構築するために。

### メソッド語彙

公開動詞とプリセットの完全な語彙（シグネチャ、パラメータ、挙動）は別ファイルに
まとめてあります — [`docs/methods.md`](./docs/methods.md)（英語の信頼できる
情報源）。

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

**オプションのエクストラ — `tokens`**: `AI_R_EXTRAS=tokens bash install.sh`（または `pip install "ai-r[tokens]"`）は [tiktoken](https://github.com/openai/tiktoken) を追加し、正確な使用量の数値をフォーマット上保存していないセッションで、より良いトークンの**見積もり**を得られるようにします。完全に任意です: これがなくても、記録されているセッションファイルからは正確な数値がそのまま得られ、フォールバックの見積もりは大まかな `chars/4` ヒューリスティックに劣化しますが、正直に `estimate` とラベル付けされます — 決してクラッシュしません。

**オプションのエクストラ — `semantic`**（`AI_R_EXTRAS=semantic bash install.sh`
または `pip install "ai-r[semantic]"` ＋ 一度きりのモデルダウンロード。これは
インストーラが自動で行います）: テキスト検索（`query`、`search_sessions`）で
`sort="semantic"` を有効化します — BM25 が返す上位 50 件の候補が**意味**で
再ソートされます。

- **モデル。** ローカルの多言語エンベディング（embeddings）モデル
  [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)
  （int8 ONNX、約 118 MB、MIT）を、torch も永続インデックスも使わず
  [onnxruntime](https://onnxruntime.ai) ＋ [tokenizers](https://github.com/huggingface/tokenizers) ＋ [numpy](https://numpy.org)
  で直接動かします。強いクロスリンガル検索（ロシア語のクエリで英語の
  セッションが見つかり、その逆も成り立つ）を小さいサイズで実現するために
  選ばれました。
- **スコアの計算方法。** BM25 が語の一致で上位 50 件を絞り込みます（これは
  品質のしきい値ではなくコスト予算です — 類似度のしきい値は意図的にありません:
  このモデル系統では無関係なテキストどうしでもスコアが ≈0.7 になります）。
  プール内での最終スコアは **意味 75 % ＋ 語の一致 25 %** — 意味を主としつつ、
  語の一致分が用語の正確なヒットを埋もれさせず、同点を分けます。
- **フェイルソフト（fail-soft）。** パッケージやモデルファイルがなければ
  `sort="semantic"` は正直に BM25 の順序へフォールバックし、その理由を返します
  （`semantic: {active: false, reason, fallback: "bm25"}`）— 決してクラッシュ
  しません。

**オプションのエクストラ — `http`**（`AI_R_EXTRAS=http bash install.sh` または
`pip install "ai-r[http]"`）は [uvicorn](https://www.uvicorn.org) を追加し、
**共有 streamable-http トランスポート**を有効化します（`mcp>=1.9.0` が必要です）。

- **なぜ。** 既定では各エージェントが自前の `ai-r-mcp` を stdio 上で起動します
  — マルチエージェントのファンアウト下ではこれは N 個のプロセスとなり、
  それぞれがコールドキャッシュでコーパスを再スキャンします（RAM 枯渇の実測された
  原因）。`AI_R_MCP_TRANSPORT=http` を指定すると、スウォームの代わりに
  localhost（既定 `127.0.0.1:8756`）上の単一の**ウォームなサーバー**を
  すべてのエージェントが共有します。`packaging/systemd/` の systemd ユニットは
  アイドル時の自己終了を伴うソケットアクティベーションを追加します。
- **セキュリティ（fail-closed）。** バインドはループバック専用です。ブラウザ
  経由の攻撃（DNS リバインディング）は SDK の Origin/Host アローリストで
  遮断されます（ループバックでは常に有効）。リモートアクセスには
  `AI_R_MCP_ALLOW_REMOTE=1` **と** トークン `AI_R_HTTP_TOKEN` の両方が必要で、
  トークンがなければ起動しません（トランスクリプトはシークレットを含むため）。
  ループバックではトークンは任意です（共有マシン上の別のローカルユーザーからの
  保護）。クライアントは `Authorization: Bearer <token>` ヘッダーを送ります。
- **つまみ（env）:**
  - `AI_R_MCP_PORT` — ポート（既定 `8756`）。
  - `AI_R_MCP_IDLE_SEC` — アイドル自己終了のしきい値。
  - `AI_R_MCP_HOST` / `AI_R_MCP_ALLOW_REMOTE` — バインドするホスト / 非ループ
    バックを許可。
  - `AI_R_HTTP_TOKEN` — bearer トークン（リモートバインドでは必須）。
  - `AI_R_HAYSTACK_CACHE_MAX` — 検索キャッシュの上限（レコード数）。
  - `AI_R_HAYSTACK_CACHE_CHARS_MAX` — 総量による上限（長時間稼働サーバーの
    RSS ヒューズ）。

どちらのエクストラも完全にオプションです: これらがなくても stdio モードと
BM25 の順序は従来どおり動作します。

## 境界: リーダーであって、ガードではない

- **読み取り専用。** エージェントのコードを実行することも、その履歴に書き込む
  ことも決してありません — 読んで返すだけです。
- **グラフなし、メモリなし。** エンティティ（ターン、呼び出し、プラン、意図）を
  取り出します。それらからナレッジグラフやメモリを構築するのはあなたの仕事で、
  ai-r の仕事ではありません。
- **アクセス制御レイヤーではない — http トランスポートを除いて。** CLI、stdio
  の MCP サーバー、パッケージに到達できる者は誰でも任意のセッションを読めます:
  それは同じローカルユーザーであり、パーサーの前に認可を置いても何も守れません。
  例外は共有 http トランスポートです: これはソケット経由で到達可能なため、
  Origin アローリストと任意の bearer トークン（リモートバインドでは必須。上記の
  `http` エクストラを参照）を備えます。それでも、信頼できないローカルプロセスが
  到達できない場所にデータを置いてください。
- **セッションの内容はデータであり、コマンドではない。** 読み手（auditor、
  summarizer）はセッションのテキストを命令ではなくデータとして扱わなければ
  なりません。[Security](docs/security.md) を参照してください。

## 受け入れ（E2E シナリオ）

公開サーフェスは e2e シナリオで網羅されており、LLM エージェントが実際に稼働中の
MCP に対して実行して検証します（pytest を補完します）。完全な一覧は
[`docs/scenarios.md`](./docs/scenarios.md) を参照してください。

<!-- gallery:start -->
## 実例: ai-r の実力

機能ごとに 1 つずつまとめた実例のギャラリー（エラー分析、危険なコマンド、ネットワークの軌跡、トークン消費、プラン内コメント、コミットのファントムチェック、エージェント横断のファイル履歴、言語横断検索、ゾンビサブエージェント、git なしの差分）: [`docs/examples/showcase-gallery.md`](./docs/examples/showcase-gallery.md)。
<!-- gallery:end -->

## 次に — ドキュメント

- メソッド語彙（動詞 + プリセット）— [`docs/methods.md`](./docs/methods.md)
  （英語 SSOT）· [`docs/methods.ru.md`](./docs/methods.ru.md)（ロシア語ミラー）
- 受け入れシナリオ（90 個の e2e）— [`docs/scenarios.md`](./docs/scenarios.md)
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

- 1100+ 個のテスト、CI はカバレッジ ≥85% を要求
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
