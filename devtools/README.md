# devtools —— 验证脚本

这里不是「单元测试」，是**对着真实游戏和地面真值跑的验收脚本**。
每个脚本都能独立运行，且会打印实际观测到的数字（不是「应该如此」的断言）。

## 一条命令跑全部

```powershell
$env:PYTHONPATH = "$PWD\src"
python devtools\verify_parser.py       # IL2CPP 元数据布局
python devtools\verify_index.py        # 索引层
python devtools\verify_fieldtypes.py   # 字段类型解析率与精度
python devtools\verify_mono.py         # Mono 解析器（对官方 .NET 库真值）
python devtools\verify_adapt.py        # 跨引擎（IL2CPP + Mono）
python devtools\verify_ue.py           # UE 提取器（对打包清单真值）
python devtools\verify_agent_js.py     # Frida 探针
python devtools\verify_mcp_tools.py    # ← MCP 工具层端到端（IL2CPP，需游戏）
python devtools\verify_mcp_mono.py     # ← MCP 工具层端到端（Mono，需游戏）
python devtools\verify_mcp_ue.py       # ← MCP 工具层端到端（UE，静态即可）
python devtools\verify_mcp_extra.py    # ← 补全工具覆盖（需游戏）
python devtools\verify_il2cpp_runtime.py   # 静态↔运行时 token 交叉验证（需游戏）
python devtools\verify_runtime.py          # 运行时层分组（需游戏）
```

带「需游戏」的会**真的启动游戏进程**并在结束时结束它，
需要 Frida + 管理员权限，且本机要有对应样本。

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

## 需要的外部样本

脚本硬编码了本机样本路径，换机器需要改：

| 样本 | 路径 | 用于 |
|---|---|---|
| Garden of Witches | `D:\Projects\Garden of Witches` | IL2CPP（17 MB 元数据） |
| Archon Soul | `D:\Projects\Archon Soul` | Mono |
| Kinny and the Cosmic Cauldron | `D:\Projects\Kinny and the Cosmic Cauldron` | Mono（多程序集） |
| CatapultHero | `D:\Projects\CatapultHero` | UE（pak v12 / utoc v8） |
| Dinoblade | `D:\Projects\Dinoblade` | UE（pak v11 / utoc v8） |
| il2cpp_dump.cs | `D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs` | IL2CPP 字段类型真值（33 万行） |

样本不在时脚本会记为「无法验证」而不是失败，不会假装通过。

## 目录

```
devtools/
├── verify_*.py            验收脚本（上面那批）
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
- `find_processes` 不总能列出正在跑的游戏，`diagnostics()` 的
  `running_game_count` 也可能是 0。测试一律用 spawn 而不是靠发现。
- 跑运行时脚本前建议先 `Get-Process -Name '<游戏名>' | Stop-Process -Force`，
  残留的同名进程会让新实例的 IL2CPP 加载异常，
  表现为「注入成功但 IL2CPP 未就绪」这种假象。
