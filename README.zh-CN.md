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
- **通过 MCP 工作（15 个工具）。** 智能体用自然语言直接调用 `ai-r`；同一份
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

- **MCP 服务器**（`ai-r-mcp`）—— 通过 JSON-RPC 提供 15 个工具，任意 MCP
  智能体都能直接调用它（推荐）。默认使用 **stdio**；也可选用**共享 http 服务器**
  （一个常驻进程供所有智能体共用，取代按智能体各起一个 stdio 进程的进程群），
  参见「快速开始」中的 `http` 可选扩展。注册——参见
  [docs/mcp-registration.md](./docs/mcp-registration.md)。
- **CLI**（`ai-r`）—— 供脚本和手动使用的子命令（`list` / `read` /
  `search` / `find-file-edits` / `find-tool-calls` / `file-frequency` /
  `detect-agent` / `export rounds`）。搜索运算符——
  [docs/search-operators.md](./docs/search-operators.md)。
- **Python SDK**（`from ai_r.parsers import ...`）—— 解析器、带类型的
  `Session`/message 模型，以及事件动词，用来构建你自己的工具。

### 方法词汇表

公开动词与预设的完整词汇表（签名、参数、行为）单独维护在
[`docs/methods.md`](./docs/methods.md)。

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

**可选扩展 —— `tokens`**：`AI_R_EXTRAS=tokens bash install.sh`（或
`pip install "ai-r[tokens]"`）会加入 [tiktoken](https://github.com/openai/tiktoken)，
让那些格式里不存精确用量数字的会话得到更准的 token **估算**。完全可选：没有它，
凡是会话文件里记录了精确数字的地方仍会照原样直接给出；回退时估算会降级为粗略的
`chars/4` 启发式，并如实标注为 `estimate`——绝不崩溃。

**可选扩展 —— `semantic`**（`AI_R_EXTRAS=semantic bash install.sh` 或
`pip install "ai-r[semantic]"` + 一次性的模型下载，安装器会自行完成）：为文本
搜索（`query`、`search_sessions`）启用 `sort="semantic"` —— BM25 给出的前 50 个
候选会按**语义**重新排序。

- **模型。** 本地多语言嵌入（embeddings）模型
  [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)
  （int8 ONNX，约 118 MB，MIT），直接通过
  [onnxruntime](https://onnxruntime.ai) + [tokenizers](https://github.com/huggingface/tokenizers) + [numpy](https://numpy.org)
  运行，无需 torch，也不建持久索引。选它是因为它在体积很小的同时跨语言检索能力强
  （俄语查询能命中英语会话，反之亦然）。
- **分数如何计算。** BM25 先按词面选出前 50 个匹配（这是成本预算，而非质量门槛
  ——刻意**不设**相似度阈值：这一族模型即便是无关文本也会得到约 0.7 的分）。在
  该候选池内，最终分数 = **75% 语义 + 25% 词面匹配**——语义为主，而词面这一份既
  能避免精确的术语命中被淹没，又能打破平局。
- **失败即软降级（fail-soft）。** 缺少相应包或模型文件时，`sort="semantic"` 会
  诚实地回退到 BM25 顺序并给出说明
  （`semantic: {active: false, reason, fallback: "bm25"}`）——绝不崩溃。

**可选扩展 —— `http`**（`AI_R_EXTRAS=http bash install.sh` 或
`pip install "ai-r[http]"`）：加入 [uvicorn](https://www.uvicorn.org)，并启用
**共享的 streamable-http 传输**（需要 `mcp>=1.9.0`）。

- **为什么。** 默认情况下每个智能体都会自己启动一个通过 stdio 的 `ai-r-mcp`——在
  多智能体扇出时这就是 N 个进程，每个都带着冷缓存、重复扫描语料库（实测正是这一点
  耗尽内存）。设置 `AI_R_MCP_TRANSPORT=http` 后，取代进程群的是 localhost 上的
  单个**常驻服务器**（默认 `127.0.0.1:8756`），被所有智能体共用；
  `packaging/systemd/` 里的 systemd 单元还加上了套接字激活与空闲自退出。
- **安全（fail-closed）。** 绑定仅限回环地址。浏览器攻击（DNS-rebinding）由 SDK
  的 Origin/Host 白名单拦截（对回环地址始终开启）。远程访问需要
  `AI_R_MCP_ALLOW_REMOTE=1` **且**令牌 `AI_R_HTTP_TOKEN`——不带令牌就不启动
  （转录内容含机密）。在回环地址上令牌是可选的（用于防范共享机器上的其他本地用户）；
  客户端发送请求头 `Authorization: Bearer <token>`。
- **可调项（env）：**
  - `AI_R_MCP_PORT` —— 端口（默认 `8756`）。
  - `AI_R_MCP_IDLE_SEC` —— 空闲自退出阈值。
  - `AI_R_MCP_HOST` / `AI_R_MCP_ALLOW_REMOTE` —— 绑定的主机 / 允许非回环地址。
  - `AI_R_HTTP_TOKEN` —— bearer 令牌（远程绑定时必填）。
  - `AI_R_HAYSTACK_CACHE_MAX` —— 按记录条数的搜索缓存上限。
  - `AI_R_HAYSTACK_CACHE_CHARS_MAX` —— 按总字符量的上限（长期运行服务器的 RSS
    保险丝）。

两个扩展都完全可选：不装它们，stdio 模式与 BM25 顺序照旧工作。

## 边界：是读者，不是守卫

- **只读。** 它绝不运行智能体的代码，也绝不写入其历史——它只读取并返回。
- **无图谱、无记忆。** 它提取实体（回合、调用、计划、意图）。用它们构建知识
  图谱或记忆是你的活儿，不是它的。
- **不是访问控制层——http 传输除外。** 任何能触及 CLI、通过 stdio 的 MCP 或该包
  的人都能读取任意会话：那是同一个本地用户，在解析器前面加权限检查也守不住什么。
  例外是共享的 http 传输：它通过套接字暴露，因此带有 Origin 白名单和可选的 bearer
  令牌（远程绑定时必填，参见上文 `http` 扩展）。无论如何，仍应把数据放在不受信任的
  本地进程够不着的地方。
- **会话内容是数据，不是命令。** 无论谁来读取（auditor、summarizer）都必须
  把会话文本当作数据、而非指令来对待。参见
  [Security](docs/security.md)。

## 验收（端到端场景）

整个公开接口都由端到端场景覆盖，这些场景由 LLM 智能体针对活跃的 MCP 逐一执行（对 pytest 形成补充）。完整清单见 [`docs/scenarios.md`](./docs/scenarios.md)。

<!-- gallery:start -->
## 示例：ai-r 实战

一个真实示例画廊——每项能力一例（错误分析、危险命令、网络踪迹、令牌消耗、计划评注、提交幻影核查、跨智能体文件历史、跨语言搜索、僵尸子智能体、无 git 的 diff）：[`docs/examples/showcase-gallery.md`](./docs/examples/showcase-gallery.md)。
<!-- gallery:end -->

## 下一步——文档

- 方法词汇表（动词 + 预设）—— [`docs/methods.md`](./docs/methods.md)
  （英文 SSOT）· [`docs/methods.ru.md`](./docs/methods.ru.md)（俄文镜像）
- 验收场景（90 个 e2e）—— [`docs/scenarios.md`](./docs/scenarios.md)
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

- 1100+ 个测试，CI 要求 ≥85% 覆盖率
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
