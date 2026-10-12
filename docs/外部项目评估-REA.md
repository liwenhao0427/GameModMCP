# 外部项目评估：morluto/rea（Reverse Engineer Anything）

> 评估时点：2026-10。评估对象：<https://github.com/morluto/rea>（本地 clone
> `E:\project\rea`，HEAD `d4e8ef749`，MIT；★ 数两个来源不一致：GitHub 页面
> 显示 11.9 万，其自身 README 自述 110,000 —— **此处不采信单一数字**）。
>
> 结论先行：**REA 对我们没有功能替代关系，但它把「Agent 用 MCP 工具做逆向」的
> 工程纪律推得比我们远。** 它有 173 个工具、23.3 万行 TypeScript，却**不做游戏
> Mod、不支持 Unity/IL2CPP/Mono**（工具契约层 `il2cpp|unity` 零命中，见 §1.3）——
> 所以不是竞品，是一面**方法论镜子**。
>
> 本次采纳 **5 条机制**（§3），明确不采纳 **6 条**（§4）；并借对照发现本项目
> **1 处真实缺陷 + 1 处待决策**（§5，均可复现）。

评估方法：本地 clone 只读通读；契约层、证据层、进程所有权层、门禁脚本逐文件核对；
3 个子 Agent 分头深挖并回填 `agent_logs/`（§7）。**文中数字均为本次在本机跑出来的，
非引用他人结论。**

---

## 1. 它是什么

### 1.1 规模（实测）

| 项 | 值 | 怎么测的 |
|---|---|---|
| 源文件 / 行数 | 1,188 个 / 232,887 行 | `src/**/*.ts`，排除 `.test.` / `.fixture.` |
| 测试文件 / 行数 | 358 个 / 86,573 行 | `src/**/*.test.ts` |
| `tests/` 代码文件 | 704 个 | 该目录共 817 个文件，此处只数 `.ts/.mjs/.c/.m` |
| 契约层 | 63 文件 / 8,910 行 | `src/contracts/` |
| 工具数 | **173** | `TOOL_EFFECTS` 键数（有测试断言它 == `TOOL_CONTRACTS` 名集） |
| MCP prompts | **6** | `PROMPT_CONTRACTS` |
| `verify:*` 通道 | **82** 条 | `package.json` scripts |

### 1.2 分层（比我们细）

```
src/domain/       纯语义：evidence / residualUnknown / obligation / 各类比较
src/contracts/    工具唯一真源：schema + effects + annotations + examples
src/application/  共享工作流（provider 无关）
src/{ghidra,hopper,ida,browser,...}/  provider 适配器
src/server/       MCP 注册层（把契约绑到 handler）
src/cli/          CLI（与 MCP 共用同一批工作流）
```

`AGENTS.md` 里写明依赖只向内流，并且**用脚本强制**（`verify-module-boundaries.mjs`：
`domain`/`contracts` 是纯层，只能向内；另有 4 类边界规则）。

### 1.3 能力边界：它**不**做我们做的事

工具契约层搜 `il2cpp|unity|MonoBehaviour`：**0 处命中**。
`docs/managed-code-analysis.md:266` 明确写：

> ReadyToRun, NativeAOT, and IL2CPP requires separately verified format and provider

即：REA 支持原生逆向（Hopper/Ghidra/IDA）、.NET 托管（静态）、JS/Electron、
浏览器、APK、固件、EVM 等，**IL2CPP 是它明确声明的未覆盖项**。而我方
`il2cpp_meta.py` 已实测支持 metadata **v24–v31**（见 `docs/开源方案复用评估.md`）。

**这确认了我们的护城河**：Unity/IL2CPP 这条垂直线（元数据索引 → 运行时 token
对账 → 探针 → 最后一公里验收）在 11.9 万★ 的通用逆向工具里仍是空白。

---

## 2. 与我们重合的（互相印证）

| 原则 | REA | GameModMCP |
|---|---|---|
| 工具唯一真源 | `TOOL_CONTRACTS` 数组（`src/contracts/toolContracts.ts:28`） | `server.py` 的 `@mcp.tool` |
| 忘表态就报错 | `toolContractMetadata` 缺效果声明即 `throw`（`toolEffects.ts:570-571`） | `verify_tool_annotations.py` 静态表双向比对 |
| 生成 > 校验 | `ensureGeneratedFile` + `--check` + CI `git diff --exit-code` | `gen_docs.py --write/--check` |
| 防漂移门禁 | `verify-test-discovery.mjs`、`architecture-guards.mjs` | `verify_tool_inventory.py` 9 处 |
| 「跳过」不等于「通过」 | `status:"skipped"` / `positive_e2e:false` 显式声明 | `samples.py` 记为「无法验证并跳过」 |
| 模拟不能证明真实 | `AGENTS.md:48`「simulated providers do not establish real-provider behavior」 | `devtools/README.md`「对着真实游戏与第三方真值跑」 |
| 动态覆盖率才作数 | `test:ci` 聚合 coverage | `report_tool_coverage.py --strict` |
| 非空转保证 | 断言「扫描集合非空」（如 `mentions >= 5`） | `verify_tool_inventory.py` 多条「非空转保证」 |

**双方独立收敛到同一套纪律** —— 这本身就是对我们方向的旁证。

---

## 3. 采纳的 5 条

### 3.1 残余未知（residual unknown）做成**一等台账** —— 最值得抄的一条

**REA 怎么做**（`src/domain/residualUnknown.ts:59-98`）：一个「解决不了的问题」是
一条带身份的持久记录，字段包括 `question` / `severity` / `domain` /
`required_authority` / `required_confidence` / `required_environment` /
`recommended_probes` / `relationships` / `resolution`，且**每条记录自身至少要写一条
`mutation_evidence`**（`predicate_type: "rea.residual-unknown-mutation"`，
`authority: "analyst-inference"`，并在 limitation 里声明「记录的是分析员意图，
不是答案的证明」）。

两个设计点特别硬：

1. **「已验证」与「撤销/超出范围」严格分离**。`resolution.disposition` 只允许
   `verified` 时要求 `evidence_ids` **至少 1 条**；`withdrawn`/`out-of-scope`
   允许 0 条（`residualUnknown.ts:45-56`）。判定入口返回**两个独立布尔**
   （`EvidenceLedger.ts:344-360`）：
   ```ts
   valid:        unknown.status === "resolved",      // 台账一致
   truthVerified: unknown.resolution?.disposition === "verified",  // 真的被证明
   ```
   合约文案直接写：「Withdrawn and out-of-scope dispositions are not truth claims.」
2. **不可变追加 + CAS**。`update_unknown` 不改旧记录，而是 `revision+1` +
   `previous_revision_digest` 串链（`residualUnknown.ts:257-278`）；写时必带
   `expected_revision`，stale 写者收到 `revision-conflict` 而非覆盖
   （`EvidenceLedger.ts:303-307`）。

**为什么对我们对症**：我们遇到「注入失败」「拿不到句柄」「字段类型认不出」时，
**只在返回里写一句 `error` 就结束了 —— 未知被丢弃了**。这直接违反 AGENTS.md
「不确定就明确说不确定并给出如何验证」。有了这套台账：

| 我们现状 | 可升级为 |
|---|---|
| `is_64: None`（没测出来） | open unknown + `recommended_probes` |
| `gc_control` 在 Mono 上 `ok:false` | out-of-scope unknown（留痕，不静默） |
| IL2CPP 泛型实例化字段偏移拿不到 | unknown + `required_authority: live-runtime` |
| `wedged: true` 注入卡死 | blocked unknown + 重启指引 |

**落地成本**：一个 `UnknownRegistry`（进程内，随会话生命周期）+ 3 个工具
（`list_unknowns` / `record_unknown` / `update_unknown`）。可直接复用我们已有的
`verify_state` 的三态（`pass`/`fail`/`unverifiable`）——`unverifiable` 就是
「未知」在验收侧的投影，**两处语义必须同源**。

### 3.2 证据身份哈希 + 保留引用（`ev_` id）

**REA 怎么做**：`evidence_id = ev_ + sha256(canonical(semantic_projection))`
（`evidence.ts:234-263`）。投影**故意剔除显示性字段**（`subject.name`、
`local_path`），**但保留 `raw_result`**。因此：

- 同一份字节在不同机器/路径下打开 → **同一个 id**（路径不是语义）；
- 规范化相同但 provider 原始输出不同 → 仍是两条记录；
- 篡改可检测（重算 id 不符即报错，`evidence.ts:158-168`）。

并支持两种输入形式共存（`evidenceInputContracts.ts:6-15`）：
内联完整证据，或 `{"kind":"retained-evidence","evidence_id":"ev_..."}` 引用
会话台账里的那条。

**为什么对我们对症**：我们最重要的证据 —— 「静态 token == 运行时 token」——
**只活在工具返回文本里**：没法引用、没法跨会话比对、没法检测篡改。
落地后可以：

- 让 Agent 用 `ev_` 引用那条对账证据，而不是把 20 万行 `class_detail` 反复塞进对话；
- 「同一份 `GameAssembly.dll` + 同一个 token + 同一个偏移」在不同样本上得到同一 id
  —— 这正是 `verify_il2cpp_runtime.py` 跨样本比对需要的东西。

**落地成本**：Python 侧一份「哪些字段算语义」白名单 + 对象键排序 + `hashlib.sha256`。
**不需要**照抄它的 RFC 8785 全套（我们是 Python，`json.dumps(sort_keys=True)` 已足够稳定）。

### 3.3 能力可用性上报（工具在、但当前不可用，要说清原因与补救）

**REA 怎么做**（`src/contracts/toolOutputSchemaPrimitives.ts:371-393`）：
`tools/list` **始终返回完整清单**，每个工具带：
```ts
available: false,
reason: "client_capability_missing"|"target_required"|"provider_missing"|
        "provider_unavailable"|"target_unsupported"|"unsupported_host",
remediation: string | null          // 怎么修
```
会话状态工具 `binary_session` 默认返回整个 `tool_availability` 清单。

**为什么对我们对症**：我们现在**要等工具失败才知道不可用**，而且失败理由散在各工具
里。而我们有若干**已知的、稳定的**不可用情形，完全可以事先说清：

| 情形 | 现状 | 应上报 |
|---|---|---|
| Mono 上 `gc_control` | 调用才知道 `ok:false` | `unsupported_host` + 「Mono 后端不支持」 |
| UE 的 `usmap` / `fname_pool` | 只在 `get_diagnostics` 的 note 里 | `target_unsupported` + 「样本中无 `.usmap`」 |
| `decompile` 需 `ilspycmd` | `get_diagnostics.decompiler.available` | `provider_missing` + 安装指引 |
| 无注入时 `runtime_*` / `hook_*` | 调用返回「没有运行时」 | `target_required` + 「先 `inject_process`」 |

**取舍**：我们**不**照搬 173 个工具都带一张清单（体积大）。**只给「有条件可用」
的工具加上报**，并把它挂进已有的 `get_diagnostics`（不新增工具，遵守契约第 3 条）。

### 3.4 传输预算：「先判定再构造」+ 超限给引用而不是截断

**REA 怎么做**（`src/server/toolResultEncoding.ts:37-66`）：**不**先 `JSON.stringify`
整个结果，而是遍历分片、边量边累加，**一旦超预算立即返回**
`{ok:false, bytesAtLeast, codeUnitsAtLeast, constraint}` —— 不把剩余部分拼出来。
成功结果放不下时**不截断**，而是转成 `resource_constraint` 并明说
「The analysis result was not truncated.」（`toolResult.ts:136-178`），同时给
`evidence_reference` / `continuation` 作为恢复路径。

**我们现状**（`cap.py`）：我们先构造完整 `payload` 再 `measure()`，而且 docstring
已经承认这是有意的取舍（「测量宁可偏大，不许偏小」）。实测最坏情况：

```
search_classes(pattern="", limit=2000)      856 KB
```

即**每次都完整序列化一遍 856 KB，只为算出一个上界**。

**可吸收的两点**：

1. **边遍历边量、越界即停**。仍保留「偏大」的安全性，但省掉一次完整序列化。
2. **不可分页的整体（如 `class_detail` 的完整字段表）不要给残缺列表，而给引用**。
   这比截断更符合我们「不许假成功」的规范 —— 截断后的字段表会让 Agent 以为
   「就这么多字段」。

**注意**：REA 的 `REA_MCP_MAX_RESPONSE_BYTES` 下限是 **10 MiB**（客户端接收缓冲），
与我们的 256 KB 不是一个量纲。**不要照搬数字**，我们有我们的依据（§cap.py docstring）。

### 3.5 进程所有权认证（只杀自己能证明属于我的进程）

**REA 怎么做**：spawn 时给子进程注入环境变量 token `REA_PROCESS_RUN_ID`
（`src/process/ProcessOwnership.ts`，1048 行 + 5 个测试文件 37 KB+）。
清理时**先扫进程表、逐进程读回 token、校验身份与父进程**，
**只有全部通过才允许 `SIGKILL`**；任何一条读不到就 **fail closed** 并保留现场：

```ts
// ProcessOwnership.ts:15
/** Identity proof required before REA may signal an owned process group. */
```

`docs/mcp-contracts.md` 原文：「REA never kills a process it cannot prove it owns.」

**为什么对我们对症**：我们 `launch_game` 用 Frida `spawn` 起游戏，
`close_session` 卸载 Hook，失败时**建议用户 `taskkill`**（`server.py:1609`）。
但：

- **我们没有 run token**，所以无法区分「我 spawn 的那个进程」与
  「用户自己开的同名进程」；
- 同机已在跑同名进程时我们**直接拒绝**（这个设计是对的），但
  **Agent 重试 / 并发场景**下仍可能作用于非自己启动的进程。

**落地成本**：中等。Frida `spawn` 支持传环境变量；清理侧用
`psutil`（**新依赖，需先确认**，见 AGENTS.md §2.4）或 Windows `wmic`/`tasklist`。
**建议分两步**：先只记录「这个 pid 是我 spawn 的」并写进会话状态，
让 `close_session` 与 `taskkill` 建议**带上这个事实**；token 校验作为后续加固。

---

## 4. 明确**不采纳**的

| 项 | 为什么不采纳 |
|---|---|
| **173 个工具的全量可用性清单** | 我们 45 个工具、单机单游戏场景，全量清单体积大而收益低。**只给有条件可用的工具加上报**（§3.3） |
| **MCP prompts（6 个引导式工作流）** | **已经调查过并否决**：`server.py:63-65` 记录「实测客户端（DSH）的 MCP 客户端实现了 `resources/list\|read`，却完全没有 prompts 相关方法」。加了也传不到模型面前 |
| **10 MiB 传输预算 / RFC 8785 全套** | 量纲不同（我们 256 KB 有实测依据）；Python 侧 `json.dumps(sort_keys=True)` 已够稳定 |
| **`compactInputSchema` 那层 schema 展平** | 它解决的是「Kimi 展开 `$ref` 后 schema 超 13 KB 被 Moonshot 拒绝」。我们的工具用 Python 类型注解声明参数，**本来就不发 `$ref`**，无此问题 |
| **provider 抽象层（Ghidra/Hopper/IDA 三选一）** | 它需要「分析引擎不在进程内、要用独立 exe + 桥」这个前提。我们是**自己注入、自己解析**，没有外部引擎可抽象 |
| **`docs/adr/` 那套架构决策记录** | 有意思，但该目录**在本地 HEAD 已被删除**（提交 `7a0c80085 docs: remove retired architecture and migration artifacts`，含 `capability-migration.md`）。**REA 自己把它删了** —— 我们不引入一个连作者都在退役的形式 |

> 最后一条值得单说：REA **会删掉退役文档**，而不是留着让后人读一份过期设计。
> 我们 `docs/` 下有 10 个历史报告（`多游戏兼容性扫描报告.md`、`验收失败清单.md` 等），
> 加起来 400+ KB。**是否该照 REA 的做法定期清理，值得单独讨论**（本文不动手）。

---

## 5. 借对照发现的本项目问题

### 5.1 `devtools/README.md` 的工具数已漂移（3 处，可复现）

```powershell
> Select-String -Path devtools\README.md -Pattern '\d+ 个工具'
行24:  python devtools\verify_tool_annotations.py # 38 个工具的语义标注（危险工具须声明，无需游戏）
行72:  ## 确认「38 个工具真的都跑过」——动态覆盖率
行213: │                          38 个工具逐个过一遍 → 逐游戏 × 逐工具可用性矩阵。
```

实际是 **45 个**（`@mcp.tool` 装饰器计数 + `verify_tool_inventory.py` 第 2 节确认）。

**根因**：门禁第 8 节**扫描全部 `*.md`**（`verify_tool_inventory.py` 的
`doc_files = [p for p in ROOT.rglob("*.md")...]`），
但**只查「已删工具名是否标注」，不查数字声明**；而数字声明检查
（第 2 节）与 `gen_docs.py` 的生成**只覆盖 `README.md`**。
（此处刻意**不引用行号** —— 写本文时该行就在 488，补完 2a 节后已变成 572；
这正是本文要谈的漂移。）

**这恰好印证了 REA 那条纪律**：手工维护的清单必然漂移，所以要么生成、要么由门禁
双向卡住。我们的 `README.md` 已被生成化，但**同一类数字在别的 `.md` 里没有护栏**。

→ **本批已修**（§5.3）。

### 5.2 5 个「只读」工具会写会话状态（需项目自己判断）

| 工具 | 标了 | 实际行为 |
|---|---|---|
| `search_classes` | `readOnlyHint=True` | `results.put()` 写句柄表 |
| `search_fields` | 同上 | 同上 |
| `search_methods` | 同上 | 同上 |
| `class_hierarchy` | 同上 | 同上 |
| `find_derived_classes` | 同上 | 同上 |

`_paged` 每次调用都 `self.results.put(...)`（`engine/index.py:1357`），
而 `ResultStore` 是 **32 条 LRU**（`index.py:376`）。也就是说：
**一次搜索能让先前拿到的句柄失效**。

**REA 的立场很明确**（`docs/mcp-contracts.md`）：

> `readOnlyHint` includes session state: an analysis call that records additive
> Evidence is marked non-read-only even when it leaves the target unchanged.

即：**改了会话状态就不算只读**，因为客户端会据此判断「重试是否无害」。
按这个标准，我们这 5 个工具的标注严格说是**偏乐观的**。

**但我不主张立刻改**，理由是这里有真实取舍：

- 我们的 `_ann` docstring 定义只读 = 「不改动任何东西（不写内存、不装 Hook、
  不落盘、不消费缓冲）」，句柄表**不在这个列表里**；
- 句柄表是**内部缓存**，不是用户可见状态；
- 改成非只读会让 5 个最常用的查询工具在客户端眼里变成「可能破坏东西」
  （`_ann` 对非只读默认 `destructiveHint=True`），**代价明显更大**。

→ **建议**：要么把这 5 个保持只读但**明确写进 docstring「会推进句柄代次」**，
要么给 `_ann` 增加一档「只读但改内部状态」。**这是一个需要项目决策的点，
本文只如实记录，不擅自改。**

### 5.3 本批已做的修复

**不只是改数字，而是堵住产生它的口子**（否则下个工具加进去又会漂）：

1. **订正**：`devtools/README.md` 的 3 处现值声明 `38 个工具` → `45 个工具`；
   两处**历史叙述**（「曾掉到 35/38」「一度停在 38/39」）**如实保留** ——
   那是当时的记录，改掉反而毁证据。
2. **新增门禁**：`verify_tool_inventory.py` 第 **2a 节**「其他**活文档**里的
   工具数量声明」。范围是 `devtools/README.md` / `skills/gamemod-mod/SKILL.md` /
   `AGENTS.md` 三份**会被 Agent 当指令读**的文档。

两个刻意的设计取舍：

- **显式排除 `docs/`**：那里的历史报告（`交接说明.md` 17 处、
  `项目梳理与优化建议.md` 14 处等）记录的是**当时**的事实。把它们也纳入检查，
  等于逼后人篡改证据。
- **区分「总数声明」与「子集数字」**：`AGENTS.md` 有「docstring 漏写了 13 个工具」
  「差的 8 个工具分两类原因」—— 13 和 8 是**差额**不是总数。
  拿它们和总数比是**假失败**，而假失败会让门禁被当成噪声忽略掉
  （**比不检查更糟**）。所以用 `SUBSET_WORDS` 显式识别。
- **历史词表共用**：新增的 `HISTORY_WORDS` 与第 8 节的「已删工具名必须同行标注」
  **共用同一份定义**。两份正则一旦漂开，同一个词在一处算历史、在另一处不算，
  门禁就会自相矛盾。

**验收（含非空转反证）**：

```
python devtools\verify_tool_inventory.py     →  通过 31 项，失败 0 项（exit 0）

# 反证：把 devtools/README.md 的 45 改回 38
→ ❌ devtools/README.md:24 写 38 个工具
   通过 30 项，失败 1 项（exit 1）
# 还原后 → 通过 31 项，失败 0 项
```

新节自带**两条非空转保证**：「扫到了活文档 3/3」+「这些活文档里确实有
『N 个工具』字样 5 处」—— 否则「没有 offenders」可能只是因为扫了个空集合。

---

## 6. 一句话总结

- **REA 不是竞品，是镜子。** 它 173 个工具、23.3 万行，却**明确不做 IL2CPP**
  —— 我们的垂直线仍然独占。
- **它比我们多**：残余未知台账（§3.1）、证据身份哈希（§3.2）、
  可用性上报（§3.3）、边量边停的预算（§3.4）、进程所有权认证（§3.5）。
- **它与我们同源**：单一真源 + 生成式文档 + 门禁非空转 + 动态覆盖率 +
  「跳过不算通过」——**双方独立收敛到同一套纪律**。
- **对照立刻产出了一个真缺陷**：`devtools/README.md` 的「38 个工具」漂移
  （§5.1），根因是**数字声明的护栏只覆盖 `README.md`**；已补上门禁（§5.3）。
- **还有一处待项目决策**：5 个「只读」工具其实会写会话状态（§5.2）——
  本文只如实记录，**不擅自改标注**。

---

## 7. 归档

3 个子 Agent 的完整 prompt 与 response 落在 `agent_logs/`：

| 文件 | 主题 |
|---|---|
| `20261012-REA-契约与效果层.md` | 工具契约单真源 / effects→annotations 派生 / schema 呈现 / 错误契约 |
| `20261012-REA-证据与未知模型.md` | Evidence 身份哈希 / bundle / residualUnknown / authority 分级 / 传输预算 |
| `20261012-REA-测试与skill打包.md` | 七层测试深度 / 82 条真实通道 / agent 评估 / skill 生成分发 / CI 门禁 |

---

## 参考

- [morluto/rea](https://github.com/morluto/rea)（MIT，11.9 万★）
- 关键文件（本地 clone 行号）：
  - `src/contracts/toolEffects.ts:544-565` —— 注解从效果派生
  - `src/domain/residualUnknown.ts:45-98` —— 残余未知字段与 disposition
  - `src/domain/evidence.ts:234-263` —— Evidence 语义哈希
  - `src/server/toolResultEncoding.ts:37-66` —— 边量边停的预算
  - `src/process/ProcessOwnership.ts:15-33` —— 进程所有权证明
  - `docs/tool-design.md`、`docs/mcp-contracts.md`、`docs/testing.md` —— 方法论文档
