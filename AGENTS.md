# AGENTS.md —— GameModMCP 项目规范

本文件是**本项目的唯一权威约定**。任何 Agent（人或 AI）在本仓库工作时都必须遵守。
与全局 `~/.dsh/AGENTS.md` 冲突时，以本文件为准（本文件更具体）。

---

## 1. 项目定位

GameMod MCP 是一个 **stdio MCP 服务器**，把「游戏 Mod 开发」能力暴露成标准 MCP 工具，
供通用 Agent（Claude Code / Codex / Cursor / 自研 Agent）调用。

支持三类引擎，方法论统一为**两阶段**：

```
静态阶段：解析元数据 → SQLite 索引        → 拿到「有哪些类/字段/方法/token」
运行时阶段：Frida 注入 → 真实偏移/地址     → 拿到「字段在哪个偏移、方法在哪个地址」
对账：静态 token == 运行时 token           → 这是「落点正确」的唯一证据
```

**核心断言（不许说反）**：

| 信息 | 静态元数据 | 运行时探针 |
|---|---|---|
| 类 / 字段 / 方法的**存在与签名** | ✅ 有 | ✅ 有 |
| 字段的**字节偏移** | ❌ **没有** | ✅ 有（`runtime_class_info`） |
| 方法的 **RVA / 绝对地址** | ❌ **没有**（只有 token） | ✅ 有（`find_hook_target`） |

> 历史教训：早期文档与 `instructions` 曾写「元数据给出字段偏移与方法 RVA」，
> 这与 `engine/adapt.py` 的 `method_address_in_metadata: False` 直接矛盾，
> 会误导 Agent 在没注入时就去算地址。**任何文档都不许再出现这种说法。**

---

## 2. 硬性规范

### 2.1 编码

- 所有文本文件必须 **UTF-8 无 BOM**。
- 写完文件后运行校验：`pwsh -ExecutionPolicy Bypass -File .\devtools\check_encoding.ps1`
- 该脚本会遍历交付目录，报出带 BOM 或非 UTF-8 的文件。

### 2.2 语言

- 与人交互、代码注释、文档正文：**中文**。
- 标识符（变量 / 函数 / 类 / 文件）：**英文**。
- 提交信息：中文。

### 2.3 不许编造

- 不确定就**明确说不确定**并给出如何验证，绝不猜一个看起来合理的值。
- 工具的返回值不许出现「假成功」：拿不到就 `ok: false` + `error`。
- 文档里的数字（工具数、条目数、命中数）必须是**跑出来的**，不是估计的。

### 2.4 依赖

- 运行期只依赖 `mcp` + `frida`（见 `requirements.txt`）。
- 引擎解析器（PE / IL2CPP / Mono / UE）**全部手写，只用标准库**。
  新增第三方依赖必须先说明理由并获得确认。

---

## 3. 架构与职责边界

```
src/gamemod_mcp/
├── server.py         MCP 工具层（@mcp.tool）。只做：参数校验 → 调下层 → 组装返回
├── engine/
│   ├── state.py      进程级单例会话（当前游戏 / 索引 / 运行时）
│   ├── discovery.py  进程与安装目录发现
│   ├── pe.py         PE 解析
│   ├── il2cpp_meta.py  global-metadata.dat 解析
│   ├── mono_meta.py    ECMA-335 程序集解析
│   ├── ue.py           Unreal 容器 / 清单 / 脚本对象提取
│   ├── adapt.py        跨引擎能力抽象（IL2CPP 与 Mono 的差异都收敛在这里）
│   └── index.py        SQLite 索引与查询
└── runtime/
    ├── session.py    Frida 会话封装（attach / spawn / hook / 事件）
    └── agent.js      注入到游戏进程内的探针（JS）
```

**分层规则**：

- `server.py` 不许写解析逻辑；逻辑放 `engine/` / `runtime/`，`server.py` 只编排。
- 引擎差异只允许出现在 `adapt.py`，不许散落到 `server.py` 或工具文档里。
- `agent.js` 里的能力必须有对应的 MCP 出口；没有出口的函数是死代码，应删除。

### 3.1 MCP 工具契约

1. **返回值**：永远是 dict，且**必定**含 `ok`（`True`/`False`）。失败带 `error`。
2. **日志**：一律走 stderr（`logutil`）。stdout 是 JSON-RPC 通道，写任何东西都会破坏协议。
3. **单一职责**：一个能力只暴露**一个**工具。
   若两个工具做同一件事（或一个是另一个的特例），**合并成一个**，
   用参数区分，而不是留给 Agent 去猜该用哪个。
4. **找不到就明说**：查询无结果返回 `total: 0` 并附说明，
   不许返回空列表让 Agent 误以为「不存在」。
5. **不返回超大结果**：任何可能上万条的返回都必须支持 `limit` / `offset` 或 `pattern` 过滤，
   并在 `has_more` 里如实标注是否被截断。

---

## 4. 工具增删改：必须同步的四处

改动任何 MCP 工具（新增 / 删除 / 改名 / 改签名）时，以下位置**必须同一次改完**：

| # | 位置 | 内容 |
|---|---|---|
| 1 | `src/gamemod_mcp/server.py` | 工具实现 + 模块 docstring 的「工具分层」列表 + `FastMCP(instructions=...)` |
| 2 | `README.md` | 「工具一览」表格 |
| 3 | `skills/gamemod-mod/SKILL.md` | 工作流、配方、引擎差异里出现的该工具 |
| 4 | `devtools/verify_mcp_*.py` | 覆盖该工具的验收断言 |

**工具清单以代码为准，文档不许硬编码数量。**
用 `python devtools/verify_tool_inventory.py` 校验三处（代码 / README / SKILL）是否一致；
该脚本不一致时返回非零退出码，是判断「文档是否实时更新」的唯一依据。

> 历史教训：`README.md:159` 曾列出并不存在的 `unhook_all`；
> `server.py:19-30` 的 docstring 漏写了 13 个工具；
> README 写「41 个工具」而实际已变。**靠人肉对照必然漂移，所以要用脚本卡住。**

---

## 5. 测试要求

测试放 `devtools/`，命名 `verify_<对象>.py`，**对着真实游戏与第三方真值跑**，
不是「断言应该如此」，而是**打印实际观测值**。

两类脚本都必须有（见 `devtools/README.md`）：

| 类型 | 验证对象 | 能发现什么 |
|---|---|---|
| `verify_<模块>.py` | 模块内部逻辑 | 解析算法错 |
| `verify_mcp_<引擎>.py` | **MCP 工具层** | 能力没接上、返回结构不对、假成功 |

**新增或修改工具时，必须补/改对应的 MCP 层验收断言。**

### 5.1 真值来源（为什么数字可信）

拿**游戏或平台自己生成的**东西当真值，不许拿自己写的第二个解析器当真值：

| 引擎 | 真值来源 |
|---|---|
| IL2CPP 结构 | 独立解析 + 内部一致性 + 逐条比对 |
| IL2CPP 字段类型 | `il2cpp_dump.cs`（游戏官方 dump，含 `/*0x20*/` 偏移注释） |
| Mono | 微软官方 `System.Reflection.Metadata`（`devtools/groundtruth/mono/`） |
| UE | 游戏自带 `Manifest_UFSFiles_Win64.txt` 打包清单 |

### 5.2 测试脚本的路径规范（重要）

样本路径集中定义，**不许散落在各脚本里硬编码**：

- `sys.path` 插入：用脚本自身位置推导，**不许写绝对的工作区路径**。
- 样本游戏目录：从 `devtools/samples.py` 读取；样本不存在时记为
  **「无法验证」并跳过**，不许假装通过。
- 缓存目录：一律 `<repo>/.cache-test/`，已被 `.gitignore` 排除。

> 历史教训：11 个脚本硬编码了旧工作区路径
> `C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src`，
> 该路径在本机已不存在 → 整套测试跑不起来。绝对路径是本项目最容易腐坏的东西。

---

## 6. 提交规范

### 6.1 自动提交

本仓库配置了 **DSH `Stop` hook**：每回合结束时自动 `git add -A` 并提交。
机制见 `devtools/hooks/`（注册在 `~/.dsh/hooks/claude-hooks.json`）。

- 该 hook 会**自行判断当前工作区是否为本仓库**，不是则静默跳过。
- 因此**不需要**在对话里手动收尾提交；但**改完一批文件后应主动确认** `git status` 干净。
- 若 hook 未生效（DSH 重启后才会加载配置），退回**手动提交**：
  回合结束前执行 `git add -A && git commit`。

### 6.2 提交信息

格式：`类型(范围): 摘要`，中文，动词开头，说清「改了什么、为什么」。

类型：`新增` / `修复` / `重构` / `文档` / `测试` / `构建` / `清理`

```
修复(runtime): ensure_runtime 早退导致指定 pid 被忽略
新增(测试): verify_tool_inventory 校验文档与代码的工具清单一致
文档(README): 移除不存在的 unhook_all，订正字段偏移的说明
```

### 6.3 绝不入库

- 元数据索引缓存（`*.sqlite`，单个实测 25 MB）
- `__pycache__/`、`bin/`、`obj/`
- 任何凭据、密钥、`.env`
- 游戏本体文件或从游戏提取的大体积产物（真值样本除外交由 `devtools/groundtruth/` 管理）

`.gitignore` 已覆盖上述内容。**入库前用 `git status --short` 与 `git diff --cached --stat` 确认。**

---

## 7. 目录约定

```
├── src/gamemod_mcp/     源码
├── devtools/            验证脚本 + 真值来源 + hooks 注册（不是"可选工具"，是交付物的一部分）
├── docs/                设计文档与优化建议
├── skills/gamemod-mod/  给 Agent 的 skill 说明（会被复制到 ~/.dsh/skills）
├── mcp-config/          客户端接入示例
├── agent_logs/          子 Agent 的 prompt/输出归档（见 §8）
└── AGENTS.md            本文件
```

---

## 8. 子 Agent 归档

派生、调用或模拟任何子 Agent（规划者、审查员、研究员等）时，
必须把**输入 prompt 与输出 response 完整**落盘：

- 路径：`./agent_logs/`
- 命名：`YYYYMMDD-HHmmss-[角色].md`

---

## 9. 禁止事项

- ❌ 在 `server.py` 里写解析逻辑。
- ❌ 工具返回值缺少 `ok` 字段，或失败时返回空数据冒充成功。
- ❌ 往 stdout 打印任何东西。
- ❌ 在文档里断言「静态元数据能给出字段偏移 / 方法 RVA」。
- ❌ 硬编码绝对工作区路径（`sys.path`、样本目录、缓存目录）。
- ❌ 新增工具/改签名却不同步 README、SKILL.md 与验收脚本。
- ❌ 把缓存、凭据、构建产物提交进 git。
- ❌ 功能重复的工具各留一个（让 Agent 猜该用哪个）。
