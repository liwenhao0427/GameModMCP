# devtools —— 验证脚本

这里不是「单元测试」，是**对着真实游戏和地面真值跑的验收脚本**。
每个脚本都能独立运行，且会打印实际观测到的数字（不是「应该如此」的断言）。

## 一条命令跑全部

```powershell
python devtools\run_all_verify.py          # ← 推荐：全部 + 动态覆盖率门禁 + 崩溃重试
python devtools\samples.py                 # 只看本机样本可用性（不跑测试）
```

`run_all_verify.py` 会自己带上覆盖率垫片、开跑前清空旧日志，
所以**一条命令给出的就是本次运行的证据**。它还会：
把「**断言失败**」与「**进程崩溃**」分开归类（`verify_agent_js`
自注入 Frida 偶发访问违例，崩了不等于产品有问题，但也不算通过），
并给自注入类脚本多一次重试预算 —— 重试过一定留痕，
不会把偶发失败洗成通过。

也可以按需单独跑：

```powershell
python devtools\verify_tool_inventory.py  # 文档与代码的工具清单是否一致（无需游戏）
python devtools\verify_tool_annotations.py # 38 个工具的语义标注（危险工具须声明，无需游戏）
python devtools\verify_handle_scope.py     # 结果句柄作用域：跨游戏旧句柄必须失效（需两个样本）
python devtools\verify_server_helpers.py   # server.py 的纯函数：命令行拆分 / 字段读取宽度（无需游戏）
python devtools\verify_response_size.py    # 响应体积：P2-4 前后对比 + 上限不变量 + 旧记录订正（无需注入）
python devtools\verify_cap.py              # 体积上限的机制：截断/句柄/字段诚实性/不丢数据（无需注入）
python devtools\verify_parser.py       # IL2CPP 元数据布局
python devtools\verify_index.py        # 索引层
python devtools\verify_lazy_handles.py # 结果句柄惰性物化（用解析条数断言证明）
python devtools\verify_subtype.py     # 继承过滤反过来查（与旧的逐条判定逐条相等）
python devtools\verify_fieldtypes.py   # 字段类型解析率与精度
python devtools\verify_mono.py         # Mono 解析器（对官方 .NET 库真值）
python devtools\verify_adapt.py        # 跨引擎（IL2CPP + Mono）
python devtools\verify_ue.py           # UE 提取器（对打包清单真值）
python devtools\verify_agent_js.py     # Frida 探针
python devtools\verify_mcp_tools.py    # ← MCP 工具层端到端（IL2CPP，需游戏）
python devtools\verify_mcp_mono.py     # ← MCP 工具层端到端（Mono，需游戏）
python devtools\verify_mcp_ue.py       # ← MCP 工具层端到端（UE，静态即可）
python devtools\verify_mcp_extra.py    # ← 补全工具覆盖（需游戏）
python devtools\verify_mcp_decompile.py # ← 反编译（薄封装 ilspycmd，需 Mono 样本）
python devtools\verify_il2cpp_runtime.py   # 静态↔运行时 token 交叉验证（需游戏）
python devtools\verify_runtime.py          # 运行时层分组（需游戏）
python devtools\verify_runtime_target.py   # 注入目标选择（假 Runtime，无需游戏）
python devtools\verify_merged_tools.py     # 合并后工具的回归（需游戏）
python devtools\verify_tool_coverage.py    # 静态：每个工具是否被脚本调用过（无需游戏）
python devtools\verify_encoding_console.py # 编码健壮性：GBK 控制台下不崩（无需游戏）
```

单跑某一个（排查用）：

```powershell
python devtools\run_all_verify.py --only verify_index
```

`--only` 只跑指定脚本，并**自动跳过动态覆盖率门禁** ——
只跑子集不可能覆盖全部工具，跑门禁只会得到一个恒假的「未覆盖」结论。

## 确认「38 个工具真的都跑过」——动态覆盖率

`verify_tool_coverage.py` 是**静态**检查（工具名有没有出现在脚本里），
它会**高估**：`sess.hook_method(...)` 这种会话对象的调用也算数。
真正「跑过没有」要用垫片记录。用 `run_all_verify.py` 跑就不必手动设环境变量；
要自己组合时这样做：

```powershell
$env:PYTHONPATH   = "$PWD\devtools\_cov"
$env:GAMEMOD_COV_OUT = "$PWD\.cache-test\tool_calls.jsonl"
Remove-Item .cache-test\tool_calls.jsonl -ErrorAction SilentlyContinue
# 然后正常跑各 verify 脚本
python devtools\report_tool_coverage.py          # 汇总：真实调用过几个、返回体多大
python devtools\report_tool_coverage.py --strict  # 有工具没跑到则退出码非零（门禁）
```

垫片（`devtools/_cov/sitecustomize.py`）只**观测**：不改参数、不改返回值、
异常原样抛出。它记录每次调用的 `ok`、`error` 与返回体字节数，
因此还能顺带暴露「哪个工具在返回超大结果」（对应契约 §3.1 第 5 条）。

当前实测：**38 / 38 全部在 MCP 层真实调用过**。

> 这个门禁不是摆设：曾把 `inject_process` 注入失败放宽成警告后，
> 覆盖率立刻掉到 **35/38**（`hook_address` / `read_field_value` /
> `gc_control` 因整段跳过而再没被调用）——静态检查完全看不出来。
> 现在这三个工具在注入失败时会改走**负路径**（验「无会话必须报错」），
> 保证仍被真实调用，并在输出里注明这比正路径的结论弱。
>
> 另一个佐证：新增 `decompile` 后，覆盖率一度停在 38/39 ——
> 正是因为只有 engine 层被调用、工具层没人调（`decompile` 当时的
> **唯一**调用者绕过了工具层）。补上 `verify_mcp_decompile.py` 才回到 39/39。
> （这里的 39 是**当时**的工具数；后来合并重复工具才降到 38，
> 所以本节前面写的是 38/38 —— 数字随工具数变，别把 39 当成现值。）
> 这印证了 §5.1 的结论：**静态检查看不出的「没接上」，动态门禁会立刻发现**。

不需要 `$env:PYTHONPATH`：每个脚本都通过 `samples.add_src_to_path()`
从自身位置推导 `src`。

带「需游戏」的会**真的启动游戏进程**并在结束时结束它，
需要 Frida + 管理员权限，且本机要有对应样本。

## 样本路径从哪来（不许硬编码）

样本路径**只在 `devtools/samples.py` 里定义**，其他脚本一律调它的 `find_*`：

| 访问器 | 样本 | 用于 |
|---|---|---|
| `find_il2cpp()` | Garden of Witches | IL2CPP（17 MB 元数据） |
| `find_mono()` | Archon Soul | Mono（单程序集） |
| `find_mono_multi()` | Kinny and the Cosmic Cauldron | Mono（多程序集） |
| `find_ue_pak_v12()` | CatapultHero | UE（pak v12 / utoc v8） |
| `find_ue_pak_v11()` | Dinoblade | UE（pak v11 / utoc v8） |
| `find_il2cpp_dump()` | `aiMod/dump/*/il2cpp_dump.cs` | IL2CPP 字段类型真值 |
| `find_mono_groundtruth()` | `devtools/groundtruth/mono/` | Mono 真值生成器 |

样本根默认是 `D:\Projects` 与 `~/Projects`，可用环境变量
`GAMEMOD_SAMPLES` 整体重定向。

样本不在时脚本记为**「无法验证」并跳过**，不会假装通过。

> 历史教训：脚本曾各自硬编码
> `C:\Users\Administrator\Documents\deepseek-harness\默认工作区\...`，
> 工作区一挪就整套跑不起来。绝对路径是本项目最容易腐坏的东西。

## 两类脚本的区别（这个区分很重要）

| | `verify_<模块>.py` | `verify_mcp_<引擎>.py` |
|---|---|---|
| 验证对象 | 模块**内部逻辑** | **MCP 工具层**——Agent 到底调不调得到 |
| 依赖 | 纯静态，多数不需要游戏 | 走 `server.py` 的工具函数 |
| 能发现什么 | 解析算法错 | 能力没接上、返回结构不对、假成功 |

**两类都必须有**。曾经出现过 `ue.py` 模块 28 项全过、
但 `ue_profile` / `ue_list_entries` 这些工具**根本不存在**，
而 `get_diagnostics` 还报 `ue.ready = true` —— 那是最糟的状态：
Agent 以为能用，一调就撞墙。只有 MCP 层的脚本能抓到这种问题。

### 还有第三类：基准（`bench_index.py`）

它**不是验收**，没有「通过/失败」，只有实测数字，所以不叫 `verify_*`，
也不进 `run_all_verify.py`。存在的理由是：性能改动最容易变成「我觉得快了」，
而本项目的规矩是「数字必须是跑出来的」。

量三件事，对应三个不同的优化方向：

| 段落 | 量什么 | 用来判断 |
|---|---|---|
| `[P1-2]` | `_load_meta` / 缓存命中 / 强制重建 / 纯 SQL 下限 | 缓存命中里有多少是白付的解析 |
| `[P1-3]` | ① SQL 取回 ② 解析这一页 ③ 解析全部进句柄 | 惰性句柄值不值得做 |
| `[P1-5]` | PE 与 UE 容器的重复解析占比 | 重复解析值多少钱 |

```powershell
python devtools\bench_index.py              # 两个引擎都量
python devtools\bench_index.py --engine il2cpp
python devtools\bench_index.py --only-dup   # 只量重复解析
```

**性能改动的验收方式是「改前改后各跑一次并对比」**，不是看它变绿。

## 需要的样本清单（实际路径见 samples.py）

| 样本 | 用于 |
|---|---|
| Garden of Witches | IL2CPP（17 MB 元数据） |
| Archon Soul | Mono |
| Kinny and the Cosmic Cauldron | Mono（多程序集） |
| CatapultHero | UE（pak v12 / utoc v8） |
| Dinoblade | UE（pak v11 / utoc v8） |
| `il2cpp_dump.cs` | IL2CPP 字段类型真值（33 万行） |

具体路径不在这里写死 —— 跑 `python devtools\samples.py` 看本机实际情况。

## 目录

```
devtools/
├── samples.py             样本路径的集中定义（唯一真值）
├── run_all_verify.py      一条命令跑全部 + 覆盖率门禁（崩溃重试并留痕）
├── verify_*.py            验收脚本（上面那批）
├── bench_index.py         性能基准（开索引 / 单次查询 / 重复解析）
├── report_tool_coverage.py 动态覆盖率汇总（配合 _cov/ 垫片）
├── _cov/sitecustomize.py  动态覆盖率垫片（只观测，不改行为）
├── probes/                一次性逆向侦察脚本（31 个）
└── groundtruth/           真值来源
    ├── mono/
    │   ├── MonoGroundTruth.csproj   .NET 真值生成器（用官方
    │   ├── Program.cs               System.Reflection.Metadata 读表，
    │   └── gt_archon.json           与我们的解析器逐表比对）
    └── (il2cpp 的真值直接用游戏的 il2cpp_dump.cs，不在此目录)
```

`probes/` 里的脚本是**过程产物**——当初用来反推 IL2CPP v31 记录布局、
找出字段类型解析规则（「比 typeIndex 小的最大 byval 键」）、
验证 pak 索引头字段顺序等等。它们**不再是验收的一部分**，
保留下来是因为：当格式出问题时，这些逐字节的侦察脚本是重新定位的第一步。

## 真值从哪来（为什么这些数字可信）

| 引擎 | 真值来源 | 可信度 |
|---|---|---|
| IL2CPP 结构 | 独立解析 + 内部一致性 + 对 dump 逐条比对 | 高 |
| IL2CPP 字段类型 | `il2cpp_dump.cs`（游戏官方 dump，含 `/*0x20*/` 偏移注释） | **最高** |
| Mono | Microsoft 官方 `System.Reflection.Metadata`（`MonoGroundTruth`） | **最高** |
| UE | 游戏自带 `Manifest_UFSFiles_Win64.txt` 打包清单 | **最高** |

三个「最高」都是**游戏或平台自己生成的**——不是我们自己写的第二个解析器。
拿第三方真值对，才算真的验证过。

## 环境说明

- 本机是**无显示环境**：游戏不渲染帧，所以每帧方法（`Update`、
> `Time.get_frameCount`）不会自然触发。
  `hook_fired_natural` 这项因此保持 ⚠️，
  Hook 的有效性改为「从注入侧主动调用目标」来证明。
  在有显示器/GPU 的机器上应当重新验证这一项。
- `find_processes` 不总能列出正在跑的游戏 —— 测试一律用 spawn，
  不靠发现。`get_diagnostics` 默认**不扫本机进程**（那一扫要 2.7 秒，
  且与 `find_game_processes` 职责重复），要列进程请单独调
  `find_game_processes`。
- 跑运行时脚本前建议先 `Get-Process -Name '<游戏名>' | Stop-Process -Force`，
  残留的同名进程会让新实例的 IL2CPP 加载异常，
  表现为「注入成功但 IL2CPP 未就绪」这种假象。
