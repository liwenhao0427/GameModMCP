---
name: gamemod-mod
description: 用 GameMod MCP 工具集做游戏 Mod：定位类/字段/方法、计算 Hook 落点、注入运行时探针、捕获调用事件、读写内存。当用户要求"做 Mod"、"改游戏数值"、"Hook 游戏函数"、"找游戏里的类/字段"、"逆向 Unity/Unreal 游戏"、"分析 IL2CPP/Mono 程序集"、"内存修改器"、"游戏辅助工具"、"反编译游戏脚本"时使用本 skill。也适用于用户提到 GameAssembly.dll、global-metadata.dat、il2cpp、Mono、Unreal、Frida、AOB/特征码扫描、字段偏移、方法 RVA 等场景。
---

# 游戏 Mod 开发（GameMod MCP）

## 先判断引擎——它决定你能做什么

**动手前第一步永远是 `inspect_game(game_dir=...)`**，
因为三件引擎的能力边界差别很大，走错路会白折腾很久：

| 引擎 | 能查类型系统？ | 能查字段偏移？ | 主打能力 |
|---|---|---|---|
| **IL2CPP** | ✅ 类/字段/方法/token | ✅ 运行时取 | 精确定位 Hook 落点 |
| **Mono** | ✅ 同上，且类型更精确 | ✅ 运行时取 | 同上 + 可直接反编译出 C# |
| **Unreal** | ❌ | ❌ | **资源容器**（pak/utoc 里有什么） |

下面第二节讲的「元数据优先」**只适用于 IL2CPP 与 Mono**。
如果你确认是 Unreal，直接跳到「引擎差异 → Unreal」与「配方 0」，
不要尝试 `search_classes`（会失败，因为没有类型元数据可查）。

---

## 核心理念：元数据优先，而不是内存扫描

> 适用于 **IL2CPP / Mono**。Unreal 请见上方说明。

这是本工具集与"传统内存修改器（Cheat Engine 式）"最根本的区别，**先理解这一点再动手**：

| | 传统 CE 式做法 | 本工具集做法 |
|---|---|---|
| 怎么找 "金币" | 反复搜索内存值、改值、再筛 | 查元数据拿到 `UserData.gold` 的**字段名与类型**，注入后取它的**真实偏移** |
| 怎么找函数 | AOB 特征码猜机器码 | 查元数据拿方法 **token**，注入后用 token 对账换算出**真实地址** |
| 稳定性 | 游戏一更新就全废 | 元数据变了才失效，且能自动重新解析 |
| 需要猜测吗 | 需要（试错） | 基本不需要（可枚举、可比对） |

**所以：优先用 `search_classes` / `class_detail` 定位，`scan_pattern` 只是符号被剥离时的兜底。**

### 元数据能给出什么、不能给出什么

⚠️ **最容易记反的一点**：静态元数据给出的是**类型系统**，不是内存落点。

| 信息 | 静态元数据 | 注入后的运行时 |
|---|---|---|
| 类 / 字段 / 方法的**存在与签名** | ✅ 有 | ✅ 有 |
| 方法的 **token** | ✅ 有 | ✅ 有 |
| 字段的**字节偏移** | ❌ **没有** | ✅ `runtime_class_info` |
| 方法的 **RVA / 绝对地址** | ❌ **没有** | ✅ `find_hook_target` |

所以路径永远是：**静态拿名字与 token → 注入 → 运行时拿偏移与地址 → 用 token 对账**。

游戏（IL2CPP/Mono）的元数据里保存了**完整的类型系统**：
类型名、字段名、字段类型、方法名、方法签名、方法 token。
运行时再把它们**实体化**成一个真实的内存布局：
- 每个类有真实的 `Il2CppClass` 结构
- 每个字段有真实的字节偏移
- 每个方法有真实的机器码地址

工具做的事就是：**离线解析元数据 → 在线问运行时真实布局 → 两边用 token 对上号**。
token 对上号这一点非常重要，它是"我挂的确实是我想挂的那个方法"的硬证据。

---

## 标准工作流

### 第 0 步：确认环境能做什么

```
get_diagnostics()
```

务必先看这个。它会告诉你：
- Frida 装没装（没有的话**所有运行时工具都不能用**，只能做静态分析）
- Mono / UE 解析器是否就绪
- **`decompiler.available`** —— 反编译能力（可选的外部 `ilspycmd`）是否可用
- 缓存目录在哪、当前会话状态

> 它**默认不扫本机进程**（那一步实测要 2.7 秒，且与 `find_game_processes` 重复）。
> 要列正在运行的游戏，直接调 `find_game_processes`。

如果 `frida.installed = false`，就**不要**尝试 `inject_process`，直接告诉用户需要装 Frida。

### 第 1 步：找到游戏

```
inspect_game(game_dir="D:\\...")   # 探测安装目录：引擎、主程序、关键文件
find_game_processes()              # 游戏已经在跑时，用它拿 pid
```

`inspect_game` 返回的 `engine` 字段决定后续路线：`il2cpp` / `mono` / `ue` / `unity`。

### 第 2 步：建立索引（静态查询的前提）

> **Unreal 游戏跳过这一步**——UE 没有可索引的类型元数据，
> `open_game` 会报「没有元数据文件」。UE 直接走 `detect_ue` / `ue_list_entries`。

```
open_game(game_dir="D:\\Projects\\Garden of Witches")
```

这一步会解析元数据并建 SQLite 缓存。**首次约 2 秒，之后 0.01 秒**（缓存键是元数据文件的 sha256）。
返回里 `index.type_count` 等数字可以用来 sanity check——比如 IL2CPP 游戏通常有一两万个类型。

⚠️ 不要反复调 `open_game`。重复选同一个游戏会复用会话（返回 `reused: true`），不会重建。

### 第 3 步：查询（此步不需要游戏在运行）

```python
search_classes(pattern="UserData")             # 找类
search_classes(subtype_of="UnityEngine.MonoBehaviour")  # 找所有组件
search_classes(only_enums=True, pattern="Currency")     # 找枚举常量表
search_fields(pattern="gold")                  # 直接搜字段名
search_methods(pattern="AddCurrency")          # 直接搜方法名
```

然后看细节：
```python
class_detail("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
# → fields: [{name, type, type_confidence}], methods: [{name, token, ...}], parent
# 注意 fields 里**没有 offset** —— 偏移要注入后从 runtime_class_info 取
```

**关键**：静态元数据里的字段**没有偏移**（偏移是运行时才定的），方法**只有 token 没有地址**。
要拿到能用的落点，必须进入运行时。

**如果元数据不够用，就去读源码（仅 Mono）**：元数据只能告诉你「有哪些类/字段/方法、签名如何」，
给不出**方法体逻辑**。想知道「这个方法到底怎么算伤害的」，对 Mono 游戏可以直接反编译：

```python
decompile(assembly=r"D:\...\_Data\Managed\Assembly-CSharp.dll", type_name="SpellEffect")
# → source: 67 行真正的 C# 源码；declared_types 是产物里声明的类型

decompile(type_name="FlyingWormConsole3.LiteNetLib.NetManager")   # 留空 assembly 用当前会话的程序集
# → 嵌套类型可传 "Outer.Inner"，会自动退让到外层并在 nested_note 里说明

decompile(output_dir=r"D:\out\full")     # 只给 output_dir = 反编译整个程序集
# → 落盘 800+ 个文件，只返回 file_count / sample_files，**不内联源码**
```

- **只支持 Mono/.NET**。IL2CPP 的 `GameAssembly.dll` 没有托管元数据，传进去会明确报错。
- 依赖是**可选**的（外部 `ilspycmd`）。先看 `get_diagnostics` 的 `decompiler.available`；
  为 false 时会返回安装指引而不是抛异常。
- 产物大时一定用 `output_dir` 落盘，别指望内联返回。

### 第 4 步：注入运行时

```python
inject_process()                              # 游戏已在运行
launch_game(exe="D:\\...\\Game.exe")          # 还没启动（能抢在初始化前挂钩）
```

`launch_game` 更强：能在对象被创建**之前**装好 Hook，适合拦截只执行一次的初始化逻辑。
但它需要等游戏加载 DLL，返回里的 `init.waited` 会告诉你等了多久。

### 第 5 步：拿真实落点

```python
runtime_class_info(name="PresetLoaderV2")
# → fields: [{name, type, offset: 32, is_static}]       ← 真实字段偏移
# → methods: [{name, token, pointer, rva_hex, in_module}] ← 真实方法地址
```

**这是整个流程的核心产出**。到这里你就有：
- 字段的字节偏移 → 可以用 `read_memory` / `read_field_value` 读写数值
- 方法的绝对地址 → 可以用 `hook_address` 挂钩

### 第 6 步：Hook 并观察

```python
hook_method(name="Time", method="get_frameCount", namespace="UnityEngine",
            args_type="", label="frame", max_calls=500)
hook_status()          # 看触发次数
drain_events()         # 取走捕获到的调用（取走即消费）
unhook()               # 收尾务必卸载
```

### 第 7 步：收尾

```python
close_session()        # 卸载 Hook + 断开注入 + 关索引
```

**务必收尾**。卸载 Hook 后游戏恢复正常行为；注入状态下游戏会一直带着探针运行。

---

## 各引擎的差异

### IL2CPP（Unity 的 AOT 后端，最常见）

- 元数据在 `<游戏>_Data/il2cpp_data/Metadata/global-metadata.dat`
- 逻辑在 `GameAssembly.dll`
- **工具支持最完整**：类型/字段/方法/token 全部可查，运行时偏移可读
- 游戏逻辑通常在 `Assembly-CSharp.dll` 对应的映像里
- 搜索时按 `assembly` 参数过滤能大幅缩小范围

### Mono（Unity 的 JIT 后端，老游戏或开发版）

- 程序集在 `<游戏>_Data/Managed/*.dll`，是标准 .NET DLL
- 主逻辑在 `Assembly-CSharp.dll`
- 运行时里 `mono-2.0-*.dll` 是标志
- 可以直接用 `decompile` 工具读源码（薄封装 ILSpy）——**比 IL2CPP 好搞得多**
  ```python
  decompile(type_name="SpellEffect")   # 单个类型，源码内联在 source 里
  ```
  反编译是 Mono 独有的能力：IL2CPP 没有托管元数据，必须先造 DummyDll（本工具集暂未实现）。
- 本工具集提供静态解析；运行时 Hook 需要走 Mono 的 `mono_*` API

### Unreal

- 特征是 `*-Win64-Shipping.exe`、`Engine/` 目录、`Content/Paks/*.pak`
- **没有类型系统可查**：UE 没有 IL2CPP 那样的集中元数据文件。
  FName 池、UObject 属性偏移、`.usmap` 都拿不到（需运行时）。
  所以**不要**在 UE 游戏上期待 `search_classes` / `search_fields` 能用。
- UE 的强项是**资源容器**，用这几个工具：
  - `detect_ue(exe=...)` —— 确认是不是 UE，看证据与置信度
  - `ue_profile(exe=...)` —— 概况：几个 pak/utoc、各多少条目、脚本模块
  - `ue_list_entries(container="....pak")` —— **列出打包了哪些资产**（最常用）
  - `ue_script_objects(exe=...)` —— exe 里的 `/Script/` 模块（字符串证据）
  - `ue_manifest(game_dir=...)` —— 打包清单（校验容器解析的真值）
- 典型用途：找本地化文本（`.locres`）、找配置、确认某功能是否被裁剪进包。
- **版本号拿不到是正常的**：工具会返回空串 + 原因，不要当成 bug。

---

## 常用配方

### 配方 0：Unreal 游戏——看它打包了什么

```python
detect_ue()                                     # 1. 确认是 UE（看 confidence 与 evidence）
ue_profile()                                    # 2. 有几个容器、各多少条目
# 3. 从 profile 的 detection.layout.utoc_files 里挑一个容器
ue_list_entries(container="....utoc", pattern=".locres")   # 4. 找本地化文本
ue_list_entries(container="....utoc", limit=50, offset=0)  # 5. 翻页看全貌
# 想知道某个资源在不在包里：
ue_list_entries(container="....utoc", pattern="AbilitySystem")
```

**要点**：
- 条目总数可能上万（实测某样本 21,439 条），**必须用 `pattern` 过滤或翻页**，
  不要一次拉全量。
- 过滤没命中时返回 `matched=0` **并附带说明**——那是"没搜到"，
  不是"包里没有"，换个关键词再试。
- 拿到的路径是**大小写敏感的原样**；和打包清单比对时要不敏感地比
  （实测清单会规范化大小写，敏感比对会少几十条）。

### 配方 1：改一个数值（比如金币）

```python
open_game(game_dir=...)
search_fields(pattern="gold")                   # 1. 找字段，看它属于哪个类
# 假设结果指向 UserData.gold (System.Int32)
class_detail("UserData")                        # 2. 确认字段与类型
inject_process()                                # 3. 注入
runtime_class_info(name="UserData")             # 4. 拿 gold 的真实偏移
# 5. 要改值得先拿到实例指针 —— 通过 Hook 一个会经过该对象的函数，
#    从 args[0] 拿到 this 指针
hook_method(name="UserData", method="AddGold", args_type="ptr,int")
drain_events()                                  # → events[0].args[0] 就是实例指针
read_field_value(instance="0x...", name="UserData", field="gold")
# 6. 确认读出来的值符合预期后再写（写错地址会立刻崩游戏）
```

**注意**：改数值是"当下的"修改，游戏重新加载存档可能覆盖。
要做持久修改，应该 Hook 住读取/写入该值的方法，在 Hook 里改逻辑。

### 配方 2：拦截一个函数、修改它的返回值

```python
find_hook_target(name="UserData_Service", method="get_storeStatSync")
# 记下返回的 token，去静态那边比对，确认是同一个方法
hook_method(name="UserData_Service", method="get_storeStatSync",
            namespace="TeamTapas.ProjectR.UserDatas.Service",
            args_type="")
drain_events()          # 看 retval
# 要改返回值需要在下发 JS 里操作 —— 用 eval_script 装一个自定义 Hook
eval_script("""
(function(){
  var target = ptr('0x...');            // 上一步拿到的 pointer
  Interceptor.attach(target, {
    onLeave: function(retval) { retval.replace(ptr(9999)); }
  });
  return 'hooked';
})()
""")
```

### 配方 3：找所有每帧执行的更新逻辑

```python
# 找游戏自己写的 MonoBehaviour 脚本（assembly 过滤掉 UnityEngine 等框架程序集）
find_derived_classes(base_full_name="UnityEngine.MonoBehaviour",
                     assembly="Assembly-CSharp", limit=500)
# 再看具体某个类有没有 Update
class_detail(full_name="PlayerController", method_name="Update")
search_methods(pattern="Update", owner="PlayerController")
```

> **类名有歧义时工具会拒绝，不会替你猜。**
> 同一个短名可能对应多个类（不同程序集各有同名类，实测 IL2CPP 样本
> 20,072 个类型里有 585 个短名是这种情况）。这时返回
> `ok: false` + `candidates`（含各自程序集），补 `assembly=` 消歧即可：
> `class_detail(full_name="Player", assembly="Assembly-CSharp")`。
> 这样做是因为「拿错类」比「报错」危险得多 —— 拿到的是**另一个类的整份字段表**，
> 据此算出的偏移必然错，而且要到运行时才暴露。

### 配方 4：确认 Hook 落点是对的（重要）

**永远做这一步**，否则你可能挂到了同名的另一个重载上：

```python
static = class_detail("PresetLoaderV2")          # 静态：token = 0x06000036
runtime = find_hook_target(name="PresetLoaderV2", method="Update")
# 比对：runtime.candidates[0].token 必须等于 static.methods 里那条的 token
```

工具的 `hook_method` 也会在返回里带 `chosen.token`，同样要核对。

### 配方 5：处理重载方法

同名不同参数的方法有不同 token。`find_hook_target` 返回的 `candidates` 里
每项都有 `param_count`，**先按参数个数认准你要的那个**：

```python
tgt = find_hook_target(name="BodyPart", method="Visualize")
for c in tgt["candidates"]:
    print(c["token"], c["param_count"], c["rva_hex"])
```

`hook_method` **支持指定重载**，两种方式：

```python
# 首选：按 token 精确指定（静态元数据里就有，最可靠）
static = class_detail(full_name="BodyPart", method_name="Visualize")  # 拿到 token
hook_method(name="BodyPart", method="Visualize", token="0x06000484")

# 次选：按参数个数（仅当各重载参数个数互不相同时可用）
hook_method(name="BodyPart", method="Visualize", param_count=1)
```

> ⚠️ 若目标方法有**多个重载而你没给 `token` / `param_count`**，
> `hook_method` 会**拒绝挂载**并返回候选清单（`ok: false`）——
> 这是故意的：盲选第一个候选会让 Hook 成功却挂错方法，
> 错误要到很久以后才以「数据不对」的形式暴露，极难排查。
> 只有候选唯一时才允许省略。
>
> 挂完**务必**核对返回里的 `chosen.token` 与静态查到的是否一致。

### 配方 6：大结果集翻页

```python
r = search_classes(pattern="Player", limit=50)
r["total"]        # 总共 697 个（**匹配总数**，不受 limit 影响）
r["handle"]       # "classes-1"
read_query_result(r["handle"], offset=500, limit=50)
search_query_result(r["handle"], pattern="Controller")   # 在结果内再过滤
```

**`total` 与 `returned` 不是一回事，`has_more` 才是截断标志**：

| 字段 | 含义 |
|---|---|
| `total` | 匹配**总数**（不受 limit 影响） |
| `returned` | 本次实际返回条数 |
| `has_more` | `True` = 被 limit 截断，还有没返回的 |

所以看到 `total=644 / returned=200 / has_more=true` 时，
意思是「一共 644 个，这次只给了 200 个」，**不要**把 200 当成总数。
`find_derived_classes` 也遵守同一约定。

---

## 坑与注意事项

### 假成功是最危险的

工具返回**必定**带 `ok` 字段：
- `ok: true` = 真的成功了
- `ok: false` + `error` = 失败了，看 `error` 说明

**不要**把 `ok: false` 当成"没数据"就往下走。失败时工具不会返回半真半假的数据。

### 事件是"取走即消费"

`drain_events()` 取走事件后就清空了。想持续统计就循环调用并累加：
```python
total = 0
for _ in range(10):
    time.sleep(1)
    ev = drain_events(limit=500)
    total += len([e for e in ev["events"] if e["phase"] == "enter"])
```

### 高频方法会刷爆事件队列

Hook `Update`、`get_frameCount` 这类每帧调用的方法时，务必设 `max_calls`。
事件队列满了会丢最旧的（返回里的 `dropped` 会告诉你丢了多少）。

### 参数类型要写对，否则可能崩游戏

`args_type` 里的类型必须与实际签名一致。不确定时：
- 先不传 `args_type`（只观察调用次数和来源）
- 再用 `class_detail` 看方法的参数类型
- 类型表：`int`/`uint`/`long`/`ulong`/`bool`/`float`/`double`/`ptr`/`string`/`cstring`

### 权限问题

注入需要权限。Windows 上通常要**管理员身份**运行 MCP 服务器。
带反调试/反注入的游戏（常见于网游、带 EAC/BattlEye 的游戏）会注入失败——
这是预期的，不要反复重试。

### 静态查询不需要游戏在运行

第 3 步的查询全部离线可用。**这让"先做功课再动手"成为可能**：
可以先把类结构摸清楚，再启动游戏。

### 写入内存是危险操作

`write_memory` / `read_field_value` 写错地址会**立刻崩溃游戏**。
规矩：**先读、确认值符合预期、再写**。写之前把原值记下来。

---

## 自查清单

动手前：
- [ ] `get_diagnostics()` 看 Frida 是否可用
- [ ] 确认游戏引擎类型（IL2CPP/Mono/UE）——**UE 走完全不同的路线**
- [ ] 若是 IL2CPP/Mono：`open_game` 成功且类型数量合理
- [ ] 若是 UE：`detect_ue()` 确认，再用 `ue_list_entries` 看容器

定位落点时（**仅 IL2CPP / Mono**）：
- [ ] 用 `class_detail` 拿到静态 token
- [ ] 用 `find_hook_target` / `runtime_class_info` 拿到运行时地址
- [ ] **比对两边的 token 是否一致**

改动前：
- [ ] 字段偏移是用 `runtime_class_info` 拿的真实偏移（不是猜的）
- [ ] 参数类型与实际签名一致
- [ ] 想清楚这是"临时改值"还是"改逻辑"

收尾时：
- [ ] `unhook()` 或 `close_session()`
- [ ] 确认游戏恢复正常（没有残留 Hook）

---

## 诊断：查不到东西怎么办

| 症状 | 可能原因 | 怎么办 |
|---|---|---|
| `search_classes` 返回空 | 名字拼错 / 需求完整命名空间 | 放宽 pattern（只搜一部分词）；试 `assembly` 过滤 |
| `class_detail` 说找不到 | 用了短名但库里有同名嵌套类型 | 用 `search_classes` 拿到完整 `nested_name` 再查 |
| 字段类型是 `None` | 该字段类型在元数据里无法解析 | 正常现象（约 10% 字段）；用偏移 + 内存读确认实际用途 |
| `inject_process` 失败 | 权限不足 / 反调试 / 进程刚启动 | 试管理员；或改用 `launch_game` |
| `runtime_class_info` 报错 | 类在游戏里还没被加载 | 先让游戏运行到相关场景（进游戏、打开对应界面） |
| Hook 装上但 0 次触发 | 目标方法在当前场景/状态不会被调 | 换个确实会执行的方法；或 `launch_game` 早期挂钩 |
| token 两边对不上 | 挂到了同名的重载 | 用 `token=` 精确指定；或 `param_count=`（见配方 5） |
| AOB 扫描 0 命中 | 特征码写错 / 段不可读 | 优先改用元数据定位；确认特征码格式（`48 8B ?? ??`） |
