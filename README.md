# GameMod MCP

**用元数据定位 Hook 落点的游戏 Mod 工具集** —— 一个 MCP 服务器，让任意通用 Agent（Claude Code / Codex / Cursor / …）都能做游戏 Mod。

支持 **IL2CPP**、**Mono (.NET)**、**Unreal** 三类引擎，38 个工具覆盖
「找游戏 → 建索引 → 查类/字段/方法 → 注入 → Hook → 读内存」全流程。

---

## 它解决什么问题

传统做法是拿内存修改器（Cheat Engine 式）反复搜值、改值、再筛。
问题在于：**每一次都是在猜**，游戏一更新全部作废，而且改完不知道为什么生效。

本工具集换一条路：**游戏自己就带着完整的类型系统**。

| | 传统 CE 式 | GameMod MCP |
|---|---|---|
| 怎么找 "金币" | 反复搜内存值、改值、再筛 | 查元数据拿到 `UserData.gold` 的**字段名与类型**，注入后取它的**真实偏移** |
| 怎么找函数 | AOB 特征码猜机器码 | 查元数据拿方法 **token**，注入后用 token 对账换算出**真实地址** |
| 稳定性 | 游戏更新即失效 | 元数据变了才失效，且自动重建缓存 |
| 靠猜吗 | 靠猜（试错） | 基本不靠猜（可枚举、可比对、可验证） |

> 注意两侧的分工：**静态元数据没有字段偏移、也没有方法地址**（只有 token）。
> 偏移与地址一律来自注入后的运行时探针，`token` 是把两侧对上的钥匙。

具体做法是两段式：

1. **离线解析元数据** —— IL2CPP 的 `global-metadata.dat`、Mono 的托管 dll。
   拿到类型、字段、方法、token 的完整清单。
2. **在线问运行时** —— 注入 Frida 探针，问游戏「这个字段真实偏移多少 /
   这个方法真实地址在哪」。两边的 **token 对上号**，就是「我挂的确实是
   我想挂的那个方法」的硬证据。

AOB 扫描（`scan_pattern`）保留，但**只是符号被剥离时的兜底**。

---

## 快速开始

### 1. 装依赖

```bash
pip install -r requirements.txt      # 只有 mcp + frida
```

引擎解析器（PE / IL2CPP / Mono）**全部手写、只用标准库**，不依赖
`pefile` / `lief` / 任何反编译框架。

一键安装（含冒烟测试）：

```powershell
# Windows
pwsh -ExecutionPolicy Bypass -File .\install.ps1
```
```bash
# macOS / Linux
bash install.sh
```

### 2. 接进你的 Agent

各客户端的现成配置在 [`mcp-config/`](mcp-config/README.md)，支持
Claude Code、Codex CLI、Cursor 以及任何标准 MCP 客户端。

以 Claude Code 为例，项目根目录建 `.mcp.json`：

```json
{
  "mcpServers": {
    "gamemod": {
      "command": "python",
      "args": ["C:/path/to/GameModMCP/src/gamemod_mcp/server.py"],
      "env": {
        "PYTHONPATH": "C:/path/to/GameModMCP/src",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

### 3. 让 Agent 加载 skill

把 [`skills/gamemod-mod/`](skills/gamemod-mod/SKILL.md) 放进你的 Agent 的
skill 目录。它包含完整工作流、各引擎差异、7 个配方（含 Unreal 路线）、以及一份
「查不到东西怎么办」的排查表——**这是让 Agent 少走弯路的关键**。

### 4. 开跑

```
你：帮我找到 Garden of Witches 里的存档服务，看看金币字段叫什么

Agent 会自动：
  get_diagnostics()         → 确认 Frida 可用
  inspect_game(...)         → 识别为 IL2CPP
  open_game(...)            → 建索引（首次约 2s，之后 10ms）
  search_fields("gold")     → 定位候选字段
  class_detail(...)         → 确认所属类与类型
  inject_process()          → 注入探针
  runtime_class_info(...)   → 拿真实字段偏移
```

---

## 工具一览（38 个）

> 本清单由 `python devtools/verify_tool_inventory.py` 与代码强制对齐：
> 数量或名字对不上就会非零退出。改工具请同步这张表。

### 发现层
| 工具 | 作用 |
|---|---|
| `find_game_processes` | 找正在运行的游戏进程 |
| `inspect_game` | 探测游戏目录：引擎类型、主程序、关键文件 |
| `inspect_binary` | 分析 PE 文件：架构、节区、导出/导入表、运行时类型 |

### 会话层
| 工具 | 作用 |
|---|---|
| `open_game` | 选定游戏并建索引（**跨引擎自动识别**） |
| `get_session` | 查看当前会话状态（排查问题先看这个） |
| `close_session` | 卸载 Hook、断开注入、关闭索引。**只有真的恢复原状才 `ok=true`**；任一步失败会 `ok=false` + `error` 并保留会话以便重试 |

### 静态查询层（**不需要游戏在运行**）
| 工具 | 作用 |
|---|---|
| `search_classes` | 搜类/结构/枚举，支持按基类与程序集过滤 |
| `class_detail` | 类的字段、方法、参数、继承链；给 `method_name` 可只看某方法的重载 |
| `search_fields` | 按字段名搜（定位「金币」「血量」这类目标）。`total` 是真实匹配总数，配合 `offset`/`limit` 翻页。`owner` 限定单个类，同名类跨程序集时会**拒绝并给候选**，可用 `assembly=` 消歧 |
| `search_methods` | 按方法名搜，返回 token。`owner` 语义与 `search_fields` 一致 |
| `class_hierarchy` | 继承体系（向上找基类 / 向下找派生类）。`direction="down"` 是**逐层**的：每条给的 `depth` 是它所在层、`parent` 是它**真实**的直接父类（早期版本把传递式后代当成直接子类，`depth`/`parent` 是错的）。单层超 500 条时 `down_has_more=true` |
| `find_derived_classes` | 所有继承自某类的类型；配 `assembly=` 可筛出游戏自己的 MonoBehaviour 脚本（**找脚本的必备工具**）。`total` 是匹配总数，被 `limit` 截断时 `has_more=true` |
| `analyze_dependency` | 类型依赖概览（近似，非精确调用图）。`referenced_count` 是真实总数，`referenced_types` 最多 100 条并带 `referenced_has_more` |
| `list_images` | 列出所有程序集/映像，支持 `offset`/`limit` 翻页并如实给 `has_more` |
| `decompile` | 反编译 Mono/.NET 程序集，拿到**真正的 C# 源码**（薄封装 `ilspycmd`）。给 `type_name` 反编译单个类型（小结果内联在 `source`）；给 `output_dir` 反编译整个程序集（只落盘 + 返回文件清单，不内联源码）。**只支持 Mono**，IL2CPP 会明确报错。依赖可选，见 `get_diagnostics` 的 `decompiler.available` |

### 结果句柄（大结果集翻页）
| 工具 | 作用 |
|---|---|
| `read_query_result` | 按句柄翻页读取上次查询的结果；给 `pattern` 即在结果集内二次过滤（原先独立的 `search_query_result` 已按契约第 3 条合并进来） |

句柄形如 `il2cpp-g3-classes-1`（`{引擎}-g{代次}-{种类}-{序号}`）。
**代次段是正确性要求**：句柄表是每个索引实例各自的、序号从 0 重来，
不带代次就会让旧句柄在切游戏后**静默读到另一个游戏的结果集**（实测过）。
所以切游戏或重建索引后旧句柄一律失效，失败时会返回 `available_handles`。
见 `devtools/verify_handle_scope.py`。

### 响应体积上限（256 KB）
**任何**工具返回都不会超过 256 KB —— 这不是各工具自觉，而是
`src/gamemod_mcp/cap.py` 在**所有**工具外面统一兜底（`@_cap_response`）。

超限时截断**最大的那个顶层列表**，并把完整列表登记成句柄：

- 返回里多出 `capped: true`、`cap_bytes`，`note` 写明「只回了前 N 条」；
- `has_more` 一律改成 `true`、`returned` 改成实际条数（**不许**明明还有却说没有）；
- `note` 里给出续读方式 `read_query_result(handle=..., offset=N)`，
  **一条数据都不会丢** —— 完整列表在句柄里。
- 已有 `handle` 的返回（`search_*`）**复用**它的句柄，不再另造一个；
  此时续读的 `offset` 是「原 offset + 已回条数」，按「已回条数」算会跳过前面的条目。

为什么必须有：契约第 5 条原先只靠各工具的 `limit` 自觉遵守，但 `limit` 上限是
2000，Agent 完全可以合法地要 2000 条 —— 实测 `search_classes('', limit=2000)`
的线上文本是 **856 KB**。截断后同一调用是 260 KB。
见 `devtools/verify_cap.py`（机制）与 `devtools/verify_response_size.py`（体积）。

### Unreal 引擎层
| 工具 | 作用 |
|---|---|
| `detect_ue` | 判断一个 exe 是不是 UE 游戏（带证据与置信度） |
| `ue_profile` | UE 游戏概况：容器、清单、脚本模块 |
| `ue_list_entries` | **列出 .pak/.utoc 里打包了哪些资产**（可按名字过滤、翻页）。返回 `returned`/`has_more`；翻页空页会说明是哪一种「空」 |
| `ue_script_objects` | exe 内的 `/Script/` 模块与对象路径。失败时按契约给 `ok=false` + `error` |
| `ue_manifest` | 读取打包清单 `Manifest_*Files*.txt`，支持 `offset`/`limit` 并有 `has_more` |

### 运行时层（需要注入游戏）
| 工具 | 作用 |
|---|---|
| `inject_process` | 注入已在运行的进程 |
| `launch_game` | 启动游戏并注入（**能抢在初始化前挂钩**） |
| `runtime_info` | 探针状态、可用 API、静态/运行时一致性自检 |
| `runtime_class_info` | **真实字段偏移**与**方法真实地址**。嵌套类型请传完整嵌套名（`search_classes` 的 `nested_name`）；短名命中多个时拒绝并给 `candidates` |
| `find_hook_target` | 定位 Hook 落点并比对 token（同样支持完整嵌套名，歧义时拒绝） |
| `hook_method` | 按类名+方法名装 Hook（用 `token` 指定重载；多候选未指定则拒绝） |
| `hook_address` | 按绝对地址装 Hook |
| `unhook` / `hook_status` | 卸载与查看 Hook（`unhook` 不带参数即卸载全部） |
| `drain_events` | 取走捕获到的调用事件 |
| `read_memory` / `write_memory` | 读写内存 |
| `read_field_value` | 按字段偏移读值 |
| `scan_pattern` | AOB 特征码扫描（兜底手段） |
| `eval_script` | 下发任意 Frida JS |
| `gc_control` | GC 控制（触发/禁用） |

### 诊断
| 工具 | 作用 |
|---|---|
| `get_diagnostics` | 环境自检：Frida、各解析器、缓存（默认不扫本机进程，见 `scan_processes`） |

---

## 资源（MCP resources）

只有一个：**`gamemod://guide`** —— 这份 MCP 自己的完整使用指南
（与 `skills/gamemod-mod/SKILL.md` 同源，逐字节相同）。

```text
gamemod://guide   text/markdown   约 24 KB
```

支持 resources 的客户端可以直接读它，不必先在系统提示里塞进这份长文档。
它也是全项目**唯一没有工具出口**的内容：其余（会话状态、引擎能力矩阵、
缓存与依赖清单）都已经由 `get_session` / `open_game` / `get_diagnostics`
返回，再开一个资源出口就是同一份数据的第二个来源（契约第 3 条）。

### 为什么没有 prompts

MCP 还有 `prompts` 这一面，本服务器**不做**，这是量过的结论而不是遗漏：

| 协议面 | 客户端（DSH）实测 | 结论 |
|---|---|---|
| `resources/list` `resources/templates/list` `resources/read` | 客户端实现了，且把资源包成给模型用的工具（模型够得着） | ✅ 做 |
| `prompts/list` `prompts/get` | 客户端**一个 prompt 方法都没有**，也没有对应的包 | ❌ 不做 |

证据：在 `E:\DSH\resources\app.asar` 里按路径检索 JS，
`dsh-mcp-client` 的请求分发 `switch` 里只有 `resources/*` 三个分支，
`prompts/*` **零命中**。加了 prompt 也传不到模型面前，
只会是一份没人读的声明 —— 与当初否决 `outputSchema` 是同一套判断方式
（见 `docs/项目梳理与优化建议.md` §4.14 附近）。

> 反过来说：**换个客户端就要重新量一次**。如果某个客户端确实消费 prompts，
> 「把 SKILL.md 挂成带 `engine` 参数的 prompt」是现成的做法。
> `devtools/verify_mcp_resources.py` 已经把「资源清单恰好只有 guide」
> 做成断言，将来补资源时会强制过一次讨论。

---

## 目录结构

```
GameModMCP/
├── README.md                    本文件
├── pyproject.toml               打包配置（pip install -e . 可用）
├── requirements.txt             运行依赖（只有 mcp + frida）
├── install.ps1 / install.sh     一键安装 + 冒烟测试
│
├── src/gamemod_mcp/
│   ├── server.py                MCP 服务器（38 个工具）
│   ├── logutil.py               日志（只走 stderr，stdout 留给 JSON-RPC）
│   ├── engine/                  静态分析层
│   │   ├── pe.py                纯标准库 PE/COFF 解析器
│   │   ├── discovery.py         进程枚举 / 引擎识别（不再做全机 Steam 扫描）
│   │   ├── il2cpp_meta.py       IL2CPP global-metadata.dat 解析器
│   │   ├── mono_meta.py         ECMA-335 元数据解析器（45 张表）
│   │   ├── ue.py                Unreal 提取器（pak / utoc / 清单 / 脚本对象）
│   │   ├── adapt.py             **引擎适配层**（让索引层不关心引擎差异）
│   │   ├── decompile.py         Mono 反编译薄封装（可选外部依赖 ilspycmd）
│   │   ├── index.py             SQLite 索引 + 结果句柄
│   │   └── state.py             会话状态（发现→索引→运行时的粘合层）
│   └── runtime/                 动态分析层
│       ├── agent.js             Frida 探针（59 个 il2cpp API）
│       └── session.py           Frida 生命周期管理
│
├── skills/gamemod-mod/SKILL.md  Agent 使用指南
├── mcp-config/                  各客户端的 MCP 配置样例
├── agent_logs/                  子 Agent 委派记录（prompt + 产出）
└── devtools/                    验证脚本（见 devtools/README.md）
    ├── verify_*.py              验收脚本（对真实游戏与地面真值）
    ├── check_encoding.ps1       编码校验（UTF-8 无 BOM）
    ├── probes/                  一次性逆向侦察脚本（格式出问题时重启定位用）
    └── groundtruth/             真值来源（MonoGroundTruth / 官方 dump）
```

---

## 引擎支持程度

| | IL2CPP | Mono (.NET) | Unreal |
|---|---|---|---|
| 静态查询类型 | ✅ 完整 | ✅ 完整 | ❌ 无类型系统可查 |
| 字段类型精度 | ⚠️ 启发式（~90%） | ✅ 精确（ECMA-335 签名） | — |
| 字段偏移 | 运行时取 | 运行时取 | ❌ 需运行时，未实现 |
| 方法地址 | 运行时取 | ⚠️ 元数据含 **IL 的 RVA**（不是机器码地址，需再算） | — |
| 运行时 Hook | ✅ | ⚠️ 需 mono_* API | — |
| **资源容器清单** | — | — | ✅ **pak/utoc 100% 准确** |
| 可反编译出源码 | ❌（已转 C++） | ✅（ILSpy/dnSpy 接近源码） | ❌ |

实测数据（真实游戏，非构造样本）：

- **IL2CPP**（Garden of Witches，17 MB 元数据）：
  20,072 类型 / 139,501 方法 / 81,872 字段 / 94 映像。
  字段类型解析率 90.10%，**与地面真值 dump 比对 27,264 条 0 错（100% 精度）**。
  建索引 2.6s，缓存复用 1.3s。
- **Mono**（Archon Soul，Assembly-CSharp.dll）：
  1,387 类型 / 6,611 方法 / 6,343 字段。
  与 Microsoft 官方 `System.Reflection.Metadata` 交叉验证 **45 张表 0 差异**，
  每类型的全名/基类/字段名/方法名 0 差异，签名解析 100%。
- **Unreal**（CatapultHero + Dinoblade 两个样本）：
  `.pak` v11/v12 索引与 `.utoc` v8 目录索引完整解析，
  **与打包清单交叉校验 4,556/4,556、4,565/4,565、7,178/7,178、18,956/18,956 全部 100% 命中**；
  含 UTF-16 中文路径（`字库`、`精灵片`、`角色`）正确解码。
  **FName 池、属性偏移、`.usmap` 未实现**——静态样本中不可得，诊断里如实标注。

---

## 可信度：怎么证明它是对的

这个项目**不靠「看起来能跑」**。`devtools/` 下有 30 个可复现的验证脚本，
每个都对着**真实游戏**和**地面真值**跑（其中 29 个进自动套件，
`verify_tool_coverage.py` 是静态导航图、不是门禁，故不参加）：

| 脚本 | 验证内容 |
|---|---|
| `verify_parser.py` | IL2CPP 元数据表结构与记录布局 |
| `verify_index.py` | 索引层：构建 / 缓存复用 / 各类查询 / 结果句柄 |
| `verify_lazy_handles.py` | 结果句柄惰性物化：用**解析条数**断言证明翻页只解析一页 |
| `verify_handle_scope.py` | 结果句柄的**作用域**：跨游戏/重开索引的旧句柄必须被拒绝，不许读到另一个结果集 |
| `verify_subtype.py` | 继承过滤反过来查：**与旧的逐条判定逐条相等** + 邻接表只建一次 |
| `verify_fieldtypes.py` | 字段类型解析率与**精度**（对 27,264 条 dump 真值） |
| `verify_adapt.py` | **跨引擎**：同一套查询 API 吃 IL2CPP + Mono |
| `verify_mono.py` | Mono 解析器，对官方 `System.Reflection.Metadata` 真值 |
| `verify_mono_coverage.py` | Mono 覆盖到的表 / 特性范围 |
| `verify_mono_robust.py` | Mono 对畸形输入的健壮性 |
| `verify_ue.py` | UE 提取器对两个真实样本（pak / utoc，对打包清单） |
| `verify_runtime.py` | Frida 运行时层 |
| `verify_runtime_target.py` | 运行时定位 Hook 落点（token 对账） |
| `verify_il2cpp_runtime.py` | 静态 token ↔ 运行时 token 交叉验证 |
| `verify_mcp_tools.py` | **通过 MCP 工具层**跑完整流程（IL2CPP） |
| `verify_mcp_mono.py` | **通过 MCP 工具层**跑完整流程（Mono） |
| `verify_mcp_ue.py` | **通过 MCP 工具层**验证 UE 能力真的接上了 |
| `verify_mcp_decompile.py` | `decompile` 工具（`ilspycmd` 薄封装） |
| `verify_mcp_extra.py` | 补全主流程没覆盖到的工具 |
| `verify_server_helpers.py` | `server.py` 的纯函数：命令行拆分（`launch_game`）与字段读取宽度（`read_field_value`） |
| `verify_response_size.py` | **响应体积**：P2-4 前后对比（用 `S.RAW_TOOLS` 调未套上限的原函数）、上限不变量、旧记录订正 |
| `verify_cap.py` | **体积上限的机制**：截断/句柄/字段诚实性/不丢数据（纯函数 + 端到端两层） |
| `verify_mcp_resources.py` | **MCP 资源层**：`gamemod://guide` 与磁盘 SKILL.md 逐字节相同、资源清单恰好只有它、找不到文件时如实说明 |
| `verify_mcp_context.py` | **P2-3 的决策门禁**（不是行为测试）：钉住「进度无消费方、取消够不着、tasks 会弄坏」这三条否决依据，变了就报红 |
| `verify_merged_tools.py` | 合并后的工具：参数区分而不是各留一个 |
| `verify_agent_js.py` | Frida 探针能编译、能加载、能 ping |
| `verify_tool_inventory.py` | 文档与代码的工具清单、脚本清单一致 |
| `verify_tool_annotations.py` | 38 个工具的**语义标注**：危险工具必须声明 `destructiveHint` |
| `verify_tool_coverage.py` | 静态扫「哪个工具没人写测试」（导航图，**会高估**） |
| `verify_encoding_console.py` | 验收脚本在 GBK 控制台不崩 |

> 上表与 `devtools/verify_*.py` 的一致性由 `verify_tool_inventory.py` 校验，
> 所以它不会像以前那样悄悄腐坏（本节曾写「10 个脚本」而实际已有 20 多个）。

几个关键的「硬证据」：

- **静态 token == 运行时 token**（50/50、17/17、9/9 全中）
  → 证明确实挂到了目标方法，而不是同名重载。
- **`PresetLoaderV2.userDataPresetFile` 运行时偏移 = 0x20**，
  与地面真值 dump 里的 `/*0x20*/` 一致 → 证明字段偏移是真的。
- **运行时映像数 94 == 静态映像数 94** → 证明探针挂到的是同一个游戏实例。
- **字段类型 27,264 条与 dump 比对 0 错** → 证明 `derived` 那一档没有蒙错。

跑法（**一条命令跑全部**，自带垫片、开跑前清空旧日志、结束时卡覆盖率门禁）：

```powershell
python devtools\run_all_verify.py
```

只跑一两个排查时用 `--only`（注意此时会**跳过**覆盖率门禁，因为子集不可能覆盖全部工具）：

```powershell
python devtools\run_all_verify.py --only verify_lazy_handles
```

> `verify_mcp_tools.py` 会**真的启动一个游戏**并在结束时结束它。
> 它需要 Frida 和管理员权限，且本机要有 Garden of Witches。
> 样本不存在的脚本会记为「无法验证」并跳过 —— **跳过不算通过**。

---

## 设计取舍

### 为什么不做 MCP 的进度、取消与 tasks

这三样都是**协议里有、但这个客户端不消费**的东西，量过之后才决定不做：

| 协议面 | 实测 | 结论 |
|---|---|---|
| `notifications/progress` | 客户端调工具时只传 `{signal, timeout}`，**从不传 `onprogress`** → 请求里没有 `progressToken` → `report_progress` 是静默 no-op；而且它是 `async`，同步工具根本调不了 | 不做 |
| `notifications/cancelled` | 客户端**确实**会传取消信号，但服务器这一侧够不着：FastMCP 对同步工具是**内联调用**（`return fn(...)`，不开线程），事件循环被占满，连取消通知都读不到 | 不做 |
| `notifications/message`（MCP logging） | 同样只有通用 SDK 里有，没有任何客户端包消费 | 不做 |
| tasks 扩展（`taskSupport`） | 客户端桥**直接抛错**：`requires task-based execution, which this bridge does not support` | 不做（声明 `required` 会主动弄坏） |

**代价是诚实的**：一个 60 秒的 `launch_game` 期间，这个服务器什么都干不了
（同步工具占着事件循环），用户在客户端上只能看着转圈。
要改就得把三个长任务改成 `async` + `anyio.to_thread.run_sync` 包一层 ——
那是结构性改造，而协议那头的收益恰好是没人消费的。

这三条依据由 `devtools/verify_mcp_context.py` 钉住：**变了就报红**，
提醒重读判断，而不是让断言变绿。

### 为什么引擎解析器全部手写，不用现成库

`pefile` / `lief` 更省事，但会带来三个问题：依赖体积、版本漂移、
以及**遇到魔改文件时无从下手**。自己写 PE 解析不到 400 行，
IL2CPP 元数据解析 1,000 行——换来的是零依赖和完全可控的错误信息。

### 为什么要有 `adapt.py` 这一层

IL2CPP 和 Mono 是**两个根本不同的元数据体系**：

- **IL2CPP**：字段/方法各自的索引轨道相互独立，字段的 `typeIndex`
  甚至不在自己的索引空间里，得靠「比它小的最大 byval 键」推算（~90% 可解）。
- **Mono**：标准 ECMA-335，类型都编码在 `#Blob` 签名里，能**精确**解出
  完整类型名（数组、泛型、指针都对）。

但两者在「类型/字段/方法的归属关系」上是**同构**的。把类型引用的
表示差异收进 `adapt.py`，索引层就能共用一套代码——这也是
`verify_adapt.py` 存在的意义：证明同一套 API 两边都跑得通。

### 为什么宁可返回 `ok: false` 也不猜

工具返回**必定**带 `ok` 字段。索引层用 `found=False` 表示「查了但没有」，
服务器层会把它转成明确的 `ok: false` + `error`。

原因很直接：**假成功是最危险的一类错误**。如果「类不存在」返回
`ok: true` + 空列表，Agent 会以为类存在但没有字段，然后基于这个
错误结论继续往下走。宁可报错。

同理，IL2CPP v31 的字段记录里**没有 flags 槽**，所以
`field_is_static` 返回 `None` 而不是猜一个 `False`——
猜出来的 `False` 会让 Agent 以为它验证过。

---

## 已知限制

- **IL2CPP 字段类型约 10% 解不出**。这是元数据格式本身的限制
  （复合类型只给到元素类型），不是 bug。工具会标记
  `type_confidence: unknown`，需要时用运行时内存读取确认。
- **反调试游戏注入会失败**。带 EAC/BattlEye 的网游基本注入不了，
  这是预期的，不要反复重试。
- **Unreal 支持受限**。UE 没有像 IL2CPP 那样集中的元数据文件，
  结构信息依赖 UE profile / `.usmap`。能力以 `get_diagnostics()` 报告为准。
- **Windows 注入通常需要管理员权限**。
- **Headless 环境验证不到自然调用**。没有显示/GPU 的机器上游戏不渲染帧，
  每帧方法不会自己触发。Hook 有效性通过从注入侧主动调用来证明。

---

## 环境要求

- **Python ≥ 3.10**（实测 3.13.1）
- `mcp ≥ 1.0`、`frida ≥ 16`（实测 17.17.0）
- 可选：`capstone`（需要看机器码时）

---

## 许可

MIT

