# ai-r

[![CI](https://github.com/pro-target/ai-r/workflows/CI/badge.svg)](https://github.com/pro-target/ai-r/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md)

> **一扇只读的窗口,纵览每个 AI 智能体的会话历史** ——
> Claude、Codex、OpenCode、Antigravity、Pi 全覆盖,经由 MCP、CLI
> 或 Python 包访问。
>
> 切换智能体而不丢上下文 · 审计任意智能体做过 *和跑过* 什么 · 一次搜遍所有会话。

```bash
# 一次查询,横扫所有智能体 —— 找到当初提到那个 auth bug 的会话
ai-r search "auth token refresh" --scope body
```

## 为什么?

每个 AI 编码智能体都自己存一份对话历史 —— 各放各的地方,各用各的格式。
Claude 和 Codex 写 JSONL,OpenCode 用 SQLite,Antigravity 把 "brain" 目录
撒得到处都是,Pi 按项目写 JSONL。于是你的工作被**按工具孤立**:换个智能体
就丢了线索,也没法问一句"另一个智能体已经试过什么了?"

`ai-r` 把这一切收拢进**一个只读接口**。让任意智能体 —— 或一段脚本、或你
自己 —— 指向任意会话,无论它是哪个工具写的。它就是你跑的所有智能体之间的
共享记忆。

## 实证 —— 我拿它来读我自己的活儿

`ai-r` 读的,正是当初造出 `ai-r` 的那些会话。横跨 **5 个智能体**、**684 条
已记录会话**,它已被调用 **约 125 次**:49 次会话读取、37 次正文搜索、
31 次列表、9 次文件编辑追踪。用得最多的是**审计** —— 派一个全新的智能体,
唯一职责就是冷眼复盘上一个智能体到底做了什么、决定了什么。这已经逮住过几个
悄悄把规划(连带把我)带偏的智能体;如今这类闪失逃不掉了。

## 适用场景

一个覆盖所有智能体的读取器,解锁了单个智能体日志做不到的工作流:

- **撞到服务商配额?切换智能体继续。** 任务进行到一半用完了 Codex 配额?
  启动 Antigravity,指向 Codex 会话,让它接着做 —— 同一任务、不同模型、
  不丢上下文。
- **上下文窗口用完了?重新开始并续接。** 开一个新会话,把上一个会话的 UUID
  交给它,说"从这里继续"。无论之前是哪个智能体写的,旧对话都可读。
- **跨智能体交接与排查。** "另一个智能体在这件事上做了什么?"能在
  Claude、Codex、OpenCode、Antigravity、Pi 之间通用,无需学五种日志布局。
- **"谁动过这个文件,何时动的?"** 某个路径的每一次修改 —— 跨所有智能体、
  所有会话 —— 都带时间戳。按时间段框定一次审计:"上周智能体对 `src/auth.py`
  做了什么?"(见 `find-file-edits`)。
- **审计智能体*跑过*什么,而不只是改过什么。** 每一次工具调用 —— shell 命令、
  文件写入、网页抓取、MCP 调用 —— 跨全部智能体,且每一次都标注了触发它的用户
  请求。"上周有没有哪个智能体跑过部署?""把 Codex 执行过的每条 shell 命令都
  列给我看。"(见 `find-tool-calls`)。
- **把会话回放成一轮 CHANGELOG。** 把会话渲染成 目标 / 状态 / 触及的文件 /
  决策 / 下一步 的 markdown —— 一份交接文档,可粘进另一个智能体或站会
  (见 `export rounds`)。
- **"我是哪个智能体,我在哪个会话里?"** 新启动的脚本或智能体可以检测自身
  的会话 UUID,然后读取自己或前一个会话以程序化续接(见 `detect-agent`、
  `detect-session`)。
- **崩溃后恢复。** 智能体挂了、终端关了、机器重启了?检测出你当时所在的会话,
  把它读回来,从它停下的地方接着干 —— 什么都没丢(见 `detect-session`)。
- **搜正文,不止标题。** "找出所有讨论过 `auth token` 的会话" —— 跨全部五个
  智能体,带片段 —— 通过带 `operator` 模式(`AND`/`OR`/`NOT`)的正文范围
  搜索。最适合回答"这个我以前是不是解决过?" —— 翻出当年的会话、直接复用那个
  修复,而不是从头再来一遍。

## 快速开始(1 条命令)

前置要求:Python 3.11+,带 `venv`(`python3-venv`)或 `pip`
(`python3-pip`/`pip3`),以及 `jq`(用于自动注册 Claude 和 Antigravity 的
MCP 配置 —— 其余的无需 `jq`)。

```bash
git clone https://github.com/pro-target/ai-r.git ~/dev/ai-r
cd ~/dev/ai-r && bash install.sh
```

就这样。安装器会:
- 默认使用按用户(per-user)模式;`opt` 模式需显式指定。
- 创建 venv,安装运行时包。
- 当对应配置文件存在时,为 **Claude**、**Codex**、**OpenCode**、
  **Antigravity** 修补 MCP 配置。
- 当不存在时,把 **Pi** 的 CLI 技能装到 `~/.agents/skills/ai-r/SKILL.md`。
- 跑冒烟测试。

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

## 架构

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

## 不只是工具,也是一个可复用的内核

`ai-r` 生来就是给人"借用"的。那些解析器、带类型的 `Session` 与消息模型,
以及安全辅助件(不可信内容处理、大小上限、感知引号的 shell 扫描),都小巧、
依赖极轻、且设计上只读。上面没有你用的智能体?把这个内核拿走,指向一种新的
日志格式,你就有了它的读取器 —— 大多数智能体都只差一个解析器模块。整个仓库
本身也是一份可运行的模板:"安全地读取每个智能体的历史。"

## 设计边界

`ai-r` 是公共核心:解析器、带类型消息、CLI 和 MCP。面向特定工作流的审阅者、
摘要器与审计器位于本仓库之外,消费解析器 API(`read_messages`)。

`ai-r` 是**读取器,而非守卫。** 任何能访问到 CLI、MCP 服务器或该包的调用方,
都可以读取任意会话 —— 解析器之前没有访问控制层。请把它放在不可信的本地调用方
够不着的地方。

会话内容是**不可信的** —— 读取器的调用方(审计器、摘要器、回放智能体)必须
把它当作数据,而非指令。见 [安全 —— 不可信的会话内容](docs/security.md)。

## 已知限制

- **Antigravity** —— 测试夹具(fixture)覆盖,加上本地存在 brain 目录时可选的
  真实数据冒烟测试。
- **Codex CLI 的 shell 编辑** —— `find_file_edits` 通过保守的、感知引号的重
  定向扫描(`>` / `>>`),从 `exec_command` / `local_shell_call` shell 命令中
  恢复 codex 的文件写入。通过 `tee` / `sed -i` / `cp` / `mv` / 仅 heredoc
  的写入检测不到;结构化编辑(`apply_patch` / `write_file`)始终可检测。

完整的解析器覆盖矩阵见 [docs/parsers.md](docs/parsers.md)。

## 用法

### 作为 MCP 服务器(推荐)

MCP 服务器会自动注册到你的智能体配置中。可用工具:

| 工具 | 用途 |
|---|---|
| `list_sessions(agent?, limit?, offset?)` | 列出可发现的会话,可选按智能体过滤。分页:`limit=0` = 不设上限;响应含 `total`/`offset`/`limit`/`truncated`。 |
| `read_session(uuid, agent, offset?, limit?)` | 读取单个会话;默认最多 100 条消息。传 `offset`/`limit` 翻页。 |
| `find_file_edits(path, agent?, since?, until?, limit?)` | 查找给定路径在所有会话中的每一次文件编辑,默认跨智能体,可按时间段(`since`/`until` ISO 8601)过滤。 |
| `find_tool_calls(tool_name?, tool_name_pattern?, agent?, since?, until?, limit?)` | 查找所有会话中的每一次工具调用 —— shell 命令、文件写入、网页抓取、MCP 调用。按工具名精确匹配(`tool_name`)或按子串匹配(`tool_name_pattern`);默认跨智能体,可按时间段过滤。每条命中都带上触发它的用户请求,字段为 `intent`。 |
| `search_sessions(query, agent?, scope?, operator?, limit?)` | 按标题和/或消息正文搜索,带 `operator` 模式(`AND`/`OR`/`NOT`)及 Google 风格的 `-term` 排除。见 [搜索运算符](#搜索运算符)。 |

**分页**(`limit`/`offset`,以及还有更多页时的 `truncated` 标志)在 MCP 工具
与 Python SDK 上都暴露 —— 见 [architecture.md](docs/architecture.md)。

### 作为 CLI(测试 / 脚本)

```bash
# list / read / search
ai-r list --agent pi
ai-r read --agent pi <session-uuid>
ai-r search "refactor"
ai-r search "pwa manifest" --scope body --operator and --agent claude

# 谁编辑过某文件,跨所有智能体,可选时间段
ai-r find-file-edits src/auth.py --since 2026-06-01 --until 2026-06-30
ai-r find-file-edits "config" --agent claude --limit 20

# 智能体跑过什么?精确工具名或子串模式,可按时间段
ai-r find-tool-calls Bash --since 2026-06-01
ai-r find-tool-calls --pattern deploy --agent codex

# 我是哪个智能体 / 在哪个会话(脚本、编排、自续接)
ai-r detect-agent --quiet          # → 例如 "claude"
ai-r detect-session --json         # → 候选会话 UUID

# 把会话渲染成 CHANGELOG 轮次(交接文档 / 回放)
ai-r export rounds <session-uuid> --include-round --output round.md
```

大多数子命令加 `--json` 可得到机器可读输出。

### 搜索运算符

`search_sessions`(MCP)与 `ai-r search`(CLI)共用同一个查询解析器和同一个
operator 参数。默认行为(`scope="title"`、`operator="AND"`、`limit=50`)与之前
仅按标题子串搜索一致。

**查询语法**

| 形式 | 示例 | 含义 |
|---|---|---|
| 裸词 | `pwa manifest` | 两个词都出现(operator 决定如何组合)。 |
| 带引号短语 | `"exact phrase"` | 单个字面词。 |
| 负向前缀 | `-claude` | Google 风格:这个词必须不出现。 |

查询中的 `AND`、`OR`、`NOT` 是字面搜索词。布尔行为由 `--operator and|or|not`
(CLI)或 `operator="AND"|"OR"|"NOT"`(MCP)选择。

**operator 模式**(决定正向词如何组合)

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
表格里)—— 第一个命中片段,最多 200 字符。

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
# 仅标题(legacy,仍为默认)
ai-r search "refactor"

# 正文搜索,所有词必须出现,排除 claude
ai-r search "pwa manifest -claude" --scope body --operator and

# 正文搜索,任一词,最多 5 条结果
ai-r search "pwa manifest" --scope body --operator or --limit 5

# 既不含这些词中任一个的所有内容
ai-r search "auth login" --scope body --operator not
```

### 作为 Python SDK

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

## MCP 注册

`ai-r-mcp` 是一个 stdio MCP 服务器。每个宿主工具注册一次。把 `USER` 换成你的
用户名(若 `ai-r-mcp` 在 `PATH` 上,也可去掉绝对路径)。**编辑宿主工具的配置
后请重启它** —— 它们都不支持热加载 MCP 变更。

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

编辑 `~/.config/opencode/opencode.jsonc`(顶层 `mcp` 对象)。OpenCode 与其他
工具有三处不同:`type` 是 `"local"`(非 `"stdio"`),`command` 是一个合并数组
(命令 + 参数在一起),环境键是 `"environment"`。

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

Pi(`@earendil-works/pi-coding-agent`)**没有 MCP 服务器配置可编辑**。它用
扩展/技能模型(`pi install <source>`、`pi config`),而非 `mcpServers` 映射,
所以 `ai-r-mcp` 无法作为 Pi 的进程内 MCP 工具注册(而在进程内启动它会违反 Pi
的设计契约)。取而代之,`install/agent-configs.sh` 把一个只读 **CLI 技能**放到
`~/.agents/skills/ai-r/` —— 一个 Pi 已在扫描的目录。该技能教模型从 Pi 的 bash
会话里调用 `ai-r` CLI,不涉及 MCP 启动。Pi 会话也完全可由 `ai-r` 通过 CLI
(`ai-r list --agent pi`、`ai-r read …`)或 Python SDK 读取;两者都直接读取
`~/.pi/agent/sessions/` 文件。要用 `/ai-r` 斜杠命令,在
`~/.pi/agent/settings.json` 里设 `enableSkillCommands: true`(即便默认 `false`,
技能文本也能用)。

### 注意事项

- `ai-r-mcp` 必须在 `PATH` 上,否则用上面的绝对路径。
- JSON 配置修补用 `jq`。若缺 `jq`,Codex、OpenCode、Pi 的注册仍会完成;Claude
  与 Antigravity 的配置会被跳过 —— 装 `jq` 或用上面的代码片段手动注册。
- 编辑配置文件后重启宿主工具。
- 服务器只读;任何能访问它的调用方都能读取任意会话。见 [设计边界](#设计边界)。

## 开发

```bash
git clone https://github.com/pro-target/ai-r.git
cd ai-r
pip install -e ".[dev]"
pytest --cov=src/ai_r
```

- 350+ 测试,CI 要求覆盖率 ≥80%。
- Conventional Commits(`feat:`、`fix:`、`docs:`、…)。
- 关于添加新智能体,见 [CONTRIBUTING.md](./CONTRIBUTING.md) 和
  [docs/parsers.md](./docs/parsers.md)。
- `src/ai_r/validators/` 与 `src/ai_r/templates/` 是可选的独立辅助
  (session-note markdown 校验),不属于 CLI 或 MCP 表面。

## 许可证

MIT —— 见 [LICENSE](./LICENSE)。
