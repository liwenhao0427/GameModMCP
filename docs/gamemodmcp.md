# GameMod MCP 后续优化设计与执行指导书（供本地 Agent 执行）

> **致本地执行 Agent 的指令**：
> 本文件是 `GameModMCP` 下一阶段的**权威优化设计与分批执行指导书**。
> 在动手修改任何一行代码前，你**必须**先完整阅读根目录的 `AGENTS.md`，并严格遵守本指导书定义的**批次边界、分层红线、非空转反证要求与五处文档同步门禁**。
> **严禁跨批次混改、严禁跳过反证测试、严禁攒到最后一次性提交**——每完成一个独立批次（Batch），必须跑通该批指定的 `devtools/verify_*.py` 与编码校验，确认全绿后立即按中文规范提交（`git commit`）。

## 0. 全局执行纪律与红线（动手前必读）

### 0.1 绝对不可触碰的架构红线
1. **零新增运行期第三方依赖**：`requirements.txt` 与 `pyproject.toml` 运行期只允许保留 `mcp>=1.0.0,<2` 与 `frida>=16.0.0`。任何 PE / IL2CPP / Mono / UE 解析及运行时探针扩展一律使用 Python 标准库与原生 Frida JS 实现。
2. **禁止为 `agent.js` 引入编译打包步骤**：`src/gamemod_mcp/runtime/agent.js` 必须保持单文件原生 ES5/ES6 脚本形态，修改后落盘即生效（受 `devtools/verify_mono_runtime.py` §1b 门禁保护）。
3. **禁止在 `server.py` 内编写解析与底层计算逻辑**：`server.py` 只负责「参数校验 → 调下层 `engine/` 或 `runtime/` → 组装返回」。引擎差异只允许出现在 `engine/adapt.py` 与 `runtime/agent.js` 的后端表（`BE_IL2CPP` / `BE_MONO`）内。
4. **禁止重提已实测否决项**：不要添加 MCP `prompts`、不要添加 `outputSchema`、不要把 45 个同步工具改成 `async def`、不要改乱 `verify_tool_annotations.py` 的手工三分组表（详见 `docs/交接与评审说明书.md` §6.3 与 §11）。

### 0.2 每个批次（Batch）的标准闭环流水线
每执行一个 Batch，必须按以下 5 步走完闭环：
1. **写非空转反证测试**：先在对应的 `devtools/verify_*.py` 中补充断言，确认在**旧代码**下新断言确实报红（证明断言不是恒真）。
2. **实施最小化代码修改**：按本指导书指定的文件与函数修改，不顺手格式化无关代码。
3. **跑本批专项验收 + 关联门禁**：运行本批指定的 `python devtools\verify_xxx.py`。若改动触及 `server.py` 的工具签名或 docstring，**必须**紧接着执行：
   ```powershell
   python devtools\gen_docs.py --write
   python devtools\verify_tool_inventory.py
   python devtools\verify_tool_annotations.py
   ```
4. **跑编码门禁**：
   ```powershell
   pwsh -ExecutionPolicy Bypass -File .\devtools\check_encoding.ps1
   ```
   确认所有新增或修改的文件均为 **UTF-8 无 BOM**。
5. **独立提交**：确认 `git status --short` 只含本批文件，按 `类型(范围): 摘要` 格式提交中文 commit。

## 1. 批次总览与优先级路线图

| 批次 | 优先级 | 主题 | 涉及核心文件 | 是否需真机游戏进程 | 预期收益 |
|---|---|---|---|---|---|
| **Batch 1** | **P0** | 修复 `agent.js` Windows x64 混合参数寄存器槽位错位（ABI 缺陷） | `src/gamemod_mcp/runtime/agent.js`<br>`devtools/verify_agent_js.py` | 否（自注入本进程即可验） | 消除整数/指针与浮点混传时第 2 个参数起全部读错寄存器的静默错值 Bug |
| **Batch 2** | **P0** | 修复 `agent.js` 中 `monoKeep` 回调泄漏并收敛双后端分支 | `src/gamemod_mcp/runtime/agent.js`<br>`devtools/verify_agent_js.py` | 否（静态 + 自注入可验） | 消除长会话下每次 RPC 泄漏一个 `NativeCallback` 的隐患，清理 RPC 层内联分支 |
| **Batch 3** | **P0** | 修复 `verify_state` 逗号切分缺陷与 `index.py` 死方法/线性扫描 | `src/gamemod_mcp/server.py`<br>`src/gamemod_mcp/engine/index.py`<br>`src/gamemod_mcp/engine/il2cpp_meta.py` | 否（纯函数可验） | 支持 `verify_state` 正则量词 `{1,3}` 与泛型类名，将建索引 `image_of_type` 从 $O(N\times M)$ 降至 $O(N)$ |
| **Batch 4** | **P0** | 修复并发工具调用下 `SessionState.runtime` 的 TOCTOU 竞态与 GC 线程保护 | `src/gamemod_mcp/engine/state.py`<br>`src/gamemod_mcp/server.py`<br>`devtools/verify_report_fixes.py` | 否（多线程桩可验） | 消除线程池并发执行时 `st.runtime` 在检查后变 `None` 的异常窗口 |
| **Batch 5** | **P1** | `server.py` 模块化瘦身与工具 docstring 上下文降噪（省 ~5,000 Token） | `src/gamemod_mcp/server.py`<br>`src/gamemod_mcp/engine/fields.py`（新）<br>`src/gamemod_mcp/engine/verifier.py`（新） | 否（门禁全量可验） | `server.py` 缩减 750+ 行，每次会话 `tools/list` 固定开销降低约 40%，且 100% 兼容现有门禁 |
| **Batch 6** | **P1** | `hook_method` / `hook_address` 扩展受控返回值覆写（`override_retval`） | `src/gamemod_mcp/runtime/agent.js`<br>`src/gamemod_mcp/runtime/session.py`<br>`src/gamemod_mcp/server.py` | 是（或自注入 C 函数） | 免手写 `eval_script` 野挂 `Interceptor`，把「改函数返回值」纳入 `hook_status` 与 `unhook` 统一监管 |
| **Batch 7** | **P1** | Mono 实例枚举补齐 `UnityEngine.Object` 路径与验收套件 `--fast` 分档 | `src/gamemod_mcp/runtime/agent.js`<br>`devtools/run_all_verify.py` | 视样本而定 | 打通 Mono 下 90% 游戏组件（`MonoBehaviour`/`ScriptableObject`）的活实例枚举；日常自检提速至秒级 |

## 2. 分批详细设计与执行规范

### Batch 1：修复 `agent.js` Windows x64 混合参数寄存器槽位错位（P0-1）

#### 2.1 问题定位与取证
* **位置**：`src/gamemod_mcp/runtime/agent.js` 中 `hookAt()` 的 `onEnter` 回调（约 L749–788）。
* **现状缺陷**：
  当前代码使用 `var intIdx = 0, fpIdx = 0;` 两个独立计数器：
  * 遇到 `'float'` / `'double'` 时读 `xmmValue(this.context, fpIdx, specI)` 并令 `fpIdx++`（**未递增 `intIdx`**）；
  * 遇到整型/指针时读 `safeArg(args, intIdx)` 并令 `intIdx++`（**未递增 `fpIdx`**）。
* **为什么在 Windows x64 下是错的**：
  对标 [frida-il2cpp-bridge](https://www.google.com/search?q=https%3A%2F%2Fgithub.com%2Fvfsfitvnm%2Ffrida-il2cpp-bridge) 与 Microsoft x64 Calling Convention：Windows x64 的前 4 个参数槽位是**按参数位置共享编号**的（Slot 0: `RCX` 或 `XMM0`，Slot 1: `RDX` 或 `XMM1`，Slot 2: `R8` 或 `XMM2`，Slot 3: `R9` 或 `XMM3`）。
  当实例方法签名为 `void TakeDamage(this, float amount, int type)`（对应 `args_type="ptr,float,int"`）时：
  * Slot 0（`this`）在 `RCX`（`args[0]`）；
  * Slot 1（`amount`）在 `XMM1`（而非 `XMM0`！旧代码 `fpIdx=0` 读了 `XMM0`，拿到的是垃圾值）；
  * Slot 2（`type`）在 `R8`（`args[2]`，而非 `RDX`！旧代码 `intIdx=1` 读了 `args[1]` 即 `RDX`，拿到的又是垃圾值）。
  只有 Linux/macOS 的 System V AMD64 ABI 才是整型寄存器与浮点寄存器独立编号。

#### 2.2 修改方案
1. 在 `agent.js` 中新增一个纯函数 `argSlotIndexes(argSpecs, platform, arch)`（抽成独立函数以便单测）：
   * 当 `platform === 'windows' && arch === 'x64'` 时，第 `i` 个参数无论是浮点还是整型，其寄存器槽位下标均为 `i`（即 `slot = i`；若 `specI` 为 `'float'`/`'double'` 且 `slot >= 4`，在 `args_error` 中如实标注「Windows x64 第 5 个及之后的浮点参数在栈上，暂不支持寄存器读取」并推入 `null`，**绝不拿 `xmm4` 冒充栈参数**）。
   * 其他平台/架构保留原有的 `intIdx` 与 `fpIdx` 独立递增逻辑。
2. 在 `hookAt()` 的 `onEnter` 中调用该函数确定每个参数的读取槽位。

#### 2.3 验收与非空转反证（`devtools/verify_agent_js.py`）
1. **注入本进程实测**：在 `verify_agent_js.py` 中，通过 `eval_script` 或 `CModule`（若环境无 `CModule` 则直接调用探针暴露的 `argSlotIndexes` 并结合 `NativeFunction` 触发真实 Hook）验证 `args_type="ptr,float,int"`：
   * 断言 Windows x64 下第 0 项走整型槽 `0`（`RCX`）、第 1 项走浮点槽 `1`（`XMM1`）、第 2 项走整型槽 `2`（`R8`）。
   * 构造一个接收 `(void* p, float f, int n)` 的原生靶子或模拟 `context = {xmm0: 1.0, xmm1: 2.5}` + `args = [0x100, 0xdead, 42]`，断言捕获到的 `args` 为 `["0x100", 2.5, 42]`。
2. **非空转反证**：断言旧的双计数器逻辑对同一输入会产出 `["0x100", 1.0, 0xdead]`（第 2、3 个参数全错），证明新断言能抓出旧 Bug。
3. **提交信息**：`修复(runtime): 修正 Windows x64 混合参数 Hook 的寄存器槽位编号`

### Batch 2：修复 `agent.js` 中 `monoKeep` 回调泄漏并收敛双后端接口（P0-3 & §10.3）

#### 2.4 问题定位与取证
* **位置 1（回调泄漏）**：`src/gamemod_mcp/runtime/agent.js` 的 `monoImages()`（约 L1147–1187）与文件末尾的 `rpc.exports` 包装器（约 L2255–2266）。
  * 每个 RPC 调用入口都会执行 `monoImagesCache = null;`。
  * 下一次调用 `monoImages()` 时，都会执行 `var cb = new NativeCallback(...)` 并 `monoKeep.push(cb)`。
  * 在一次几百步的 Agent 会话中，`monoKeep` 数组会累积几百个 `NativeCallback` 实例，永不释放。
* **位置 2（后端分支散落）**：`readStaticFieldBytes`（L1712）与 `writeStaticFieldBytes`（L1786）内部仍写着 `if (be.kind === 'il2cpp') { ... } else { ... }`，违反了本文件「引擎差异全部收敛在 `BE_IL2CPP` 与 `BE_MONO` 字典内」的设计初衷。

#### 2.5 修改方案
1. **将 `mono_assembly_foreach` 的回调改为惰性单例**：
   * 在模块级定义 `var monoAssemblyCallback = null;` 与 `var monoEnumScratch = null;`。
   * 首次进入 `monoImages()` 时创建一次 `NativeCallback` 并存入 `monoAssemblyCallback`（也可放入 `monoKeep` 仅此 1 个元素），后续每次调用只需将临时收集数组赋给 `monoEnumScratch`，调用 `mono_assembly_foreach(monoAssemblyCallback, ptr(0))` 后取走结果并将 `monoEnumScratch = null`。
2. **把静态字段底层读写收进 `BE_IL2CPP` 与 `BE_MONO`**：
   * 在 `BE_IL2CPP` 上实现 `readStaticRaw(hit, buf)` 与 `writeStaticRaw(hit, buf)`；
   * 在 `BE_MONO` 上实现同名方法（内部处理 `mono_class_vtable` 与 `mono_field_static_get/set_value`）；
   * `readStaticFieldBytes` 与 `writeStaticFieldBytes` 只调 `be.readStaticRaw(hit, buf)` / `be.writeStaticRaw(hit, buf)`，消除 `if (be.kind === 'il2cpp')` 分支。

#### 2.6 验收与非空转反证（`devtools/verify_agent_js.py` & `devtools/verify_mono_runtime.py`）
1. **回调不泄漏断言**：在 `verify_mono_runtime.py`（及 `verify_agent_js.py` 的源码契约检查）中，断言 `monoImages` 内不再每次 `monoKeep.push`，且连续多次 RPC 调用后 `monoKeep.length <= 1`。
2. **后端收敛断言**：在 `verify_mono_runtime.py` §1 中增加源码扫描断言：`readStaticFieldBytes` 与 `writeStaticFieldBytes` 函数体内不得出现 `be.kind ===` 分支。
3. **提交信息**：`重构(runtime): 复用 Mono 程序集枚举回调防泄漏并收敛静态字段后端接口`

### Batch 3：修复 `verify_state` 逗号切分缺陷与 `index.py` 死代码/线性扫描（P0-5, P0-6, P0-7）

#### 2.7 问题定位与取证
1. **`verify_state` 逗号切分 Bug（`src/gamemod_mcp/server.py` L1564）**：
   * 现状：`items = [x.strip() for x in str(checks).split(",") if x.strip()]`。
   * 后果：当 `checks` 中包含正则量词（如 `log=D:\a.log#金币\s*\d{1,5},ocr=金币`）或带逗号的文本时，`split(",")` 会把 `5}` 切成一个独立条目，报出「写法不对：应是 kind=参数」的假错误。
2. **`index.py` 的 `_resolve_param` 隐蔽 Bug 与死代码（`src/gamemod_mcp/engine/index.py` L621–650）**：
   * 现状：`parameters` 表只有 `(idx, name, type_index)` 三列（L463），根本没有 `sequence` 列；而 `_resolve_param` 在 Mono 分支里写了 `idx = param_row["sequence"] - 1`，一旦调用必抛 `IndexError` 并被 `except Exception:` 静默吞成 `None`。同时全仓库已无任何地方调用 `_resolve_param`（参数解析已在 `class_detail` 中走 `adapt.resolve_param_types`）。
3. **`il2cpp_meta.py` 的 `image_of_type` 线性扫描（`src/gamemod_mcp/engine/il2cpp_meta.py` L877）**：
   * 现状：每次调用遍历 `self.images`（约 100~200 个映像），建索引时对 20,072 个类型调用 20,072 次，产生约 200~400 万次无谓循环。

#### 2.8 修改方案
1. **升级 `verify_state` 的条目切分器 `_split_checks(checks: str) -> list[str]`**：
   * 用正则 `re.split(r",(?=\s*(?:log|ocr|window|field|instances)\s*=)", str(checks))` 替代裸 `.split(",")`——只有当逗号后面紧跟着合法的 `kind=` 前缀时才切分条目；同时保留对未知 `kind`（如 `nope=x`）在首条或用 `,` 连接时的识别能力（更稳妥的做法：用 `re.split(r",(?=\s*[A-Za-z_][A-Za-z0-9_]*\s*=)", str(checks))`，因为正则量词 `{1,3}` 逗号后是数字、`Foo<A, B>` 逗号后无 `=`，而任何 `kind=spec` 逗号后都是 `标识符=`！）。
2. **清理 `index.py` 的 `_resolve_param` 死方法**：
   * 按 `AGENTS.md §3`「死代码应删除」原则，删除 `Index._resolve_param`，避免留着一个访问不存在的 `sequence` 列的坏函数误导后续维护者。
3. **优化 `il2cpp_meta.py` 的 `image_of_type`**：
   * 在 `_parse_images()` 结束时，按 `img.type_start` 升序构建 `self._image_starts: list[int]` 与对应的 `self._images_sorted: list[ImageDef]`；
   * `image_of_type(type_def)` 用 `bisect.bisect_right(self._image_starts, type_def.index) - 1` 定位候选映像，再核对 `img.type_start <= type_def.index < img.type_start + img.type_count`，将单次查找降为 $O(\log M)$。

#### 2.9 验收与非空转反证
1. 在 `devtools/verify_verify_state.py` 新增第 8 节：
   * 测试 `checks=f"log={log_hit}#精核\\s*=\\s*\\d{{1,3}},window=SomeWindow"`，断言 `total == 2` 且 `verdict == "pass"`；
   * 测试未知 kind `checks="nope=1,ocr=x"` 仍能被切成 2 条并正确报「未知的 kind」；
   * **非空转反证**：用旧写法 `checks.split(",")` 切上面那条带 `{1,3}` 的输入会得到 3 段（含 `"3}"`），证明新旧行为确实不同。
2. 在 `devtools/verify_index.py`（或 `verify_parser.py`）中：
   * 断言优化后的 `image_of_type(t)` 与线性扫描在所有 `meta.types` 上返回结果 **100% 相同**。
3. **提交信息**：`修复(server,engine): 支持 verify_state 含逗号正则并优化映像归属查找`

### Batch 4：修复并发工具调用下 `SessionState.runtime` 的 TOCTOU 竞态（P0-4）

#### 2.10 问题定位与取证
* **位置**：`src/gamemod_mcp/engine/state.py` 与 `src/gamemod_mcp/server.py` 的 16 个运行时工具。
* **现状缺陷**：
  `server.py` 已经通过 `_install_event_loop_guard()` 把所有同步工具丢进线程池并发执行。但 `server.py` 中的运行时工具写法是：
  ```python
  st = state.state()
  if not st.has_runtime():
      return _err(...)
  res = st.runtime.runtime_info()  # ← 若此时另一线程正在 close_session 或切换目标，st.runtime 已变为 None！
  ```
  这存在典型的 Check-Then-Act（TOCTOU）竞态：检查 `st.has_runtime()` 为真后，下一行访问 `st.runtime.xxx` 时 `st.runtime` 可能已被另一线程置为 `None`，抛出未预期的 `'NoneType' object has no attribute ...`。

#### 2.11 修改方案
1. 在 `SessionState`（`src/gamemod_mcp/engine/state.py`）中新增原子获取方法 `live_runtime() -> Any | None`：
   ```python
   def live_runtime(self) -> Any | None:
       """在锁内原子取出当前活着的 RuntimeSession；没有或已失效则返回 None。"""
       with self.lock:
           rt = self.runtime
           if rt is not None and getattr(rt, "alive", False):
               return rt
           return None
   ```
2. 在 `server.py` 中新增小工具 `_need_runtime()`（与现有的 `_need_index()` 对称！）：
   ```python
   def _need_runtime(hint: str = "请先调用 inject_process 或 launch_game。") -> tuple[Any, dict | None]:
       rt = state.state().live_runtime()
       if rt is None:
           return None, _err(f"当前没有已注入的运行时会话。{hint}".rstrip())
       return rt, None
   ```
3. 将 `server.py` 中所有 `if not st.has_runtime(): ... st.runtime.xxx()` 替换为 `rt, err = _need_runtime(); if err: return err; rt.xxx()`。即便并发线程在 1 毫秒后调用了 `close_session()` 清空了 `st.runtime`，当前线程局部变量 `rt` 依然指向有效的 `RuntimeSession` 实例，且其内部 `_call` 有 `_rpc_lock` 与 `_check()` 保护，绝不会爆 `NoneType` 异常。

#### 2.12 验收与非空转反证（`devtools/verify_report_fixes.py`）
1. 在 `verify_report_fixes.py` §1 后面追加并发竞态测试：
   * 构造一个假 `RuntimeSession`，起两个线程循环 200 次：线程 A 反复调用 `S.runtime_info()` / `S.hook_status()`，线程 B 反复在 `st.runtime = fake_rt` 与 `st.close_runtime()` 之间切换。
   * 断言线程 A 的 200 次调用全部返回合法的 dict（`ok: True` 或 `ok: False`），**0 次出现 `"NoneType"` 错误**。
   * **非空转反证**：模拟旧的「先调 `has_runtime()`、释放锁后再读 `st.runtime.hook_status()`」写法，在切换穿插时必触发 `AttributeError: 'NoneType' object has no attribute 'hook_status'`。
2. **提交信息**：`修复(server,state): 原子获取运行时会话引用，消除并发工具调用的 TOCTOU 竞态`

### Batch 5：`server.py` 模块化瘦身与工具 docstring 上下文降噪（P1 / §10.1 & §10.4）

#### 2.13 问题定位与取证
* **痛点 1（上下文税过高）**：当前 45 个工具的 `tools/list` 描述 + Schema 占约 **44 KB（~1.3 万 Token）**，每次新建会话都固定消耗这笔预算。而 `devtools/gen_docs.py` 的 `first_paragraph()` **只读取每个工具 docstring 的第一段**来生成 `README.md` 的「工具一览」表！其余第二段至末尾的大段「历史踩坑故事」（如 `decompile`、`wait_for`、`verify_state`、`read_query_result` 各自长达 40~80 行的长篇故事）全数与 `skills/gamemod-mod/SKILL.md`（即 `gamemod://guide` 资源）重复。
* **痛点 2（`server.py` 4,208 行过于臃肿）**：字段宽度/编码/静态字段读写（`_FIELD_WIDTH`、`_encode_field_value`、`_resolve_enum_underlying`、`_read_static_field`、`_write_static_field` 等约 450 行）与验收条件求值（`_eval_condition`、`_eval_condition_inner`、`_values_equal` 等约 300 行）属于引擎/业务辅助逻辑，塞在 `server.py` 里拉长了工具编排文件。

#### 2.14 修改方案（严守门禁兼容）
1. **抽离两个内部辅助模块（并在 `server.py` 保留同名绑定以兼容现有测试）**：
   * 新建 `src/gamemod_mcp/engine/fields.py`：迁入 `_FIELD_WIDTH`、`_FIELD_WRITE_KIND`、`_field_read_width`、`_field_write_kind`、`_encode_field_value`、`_resolve_enum_underlying`、`_explain_bytes`、`_static_field_hit`、`_read_static_field`、`_write_static_field`。
   * 新建 `src/gamemod_mcp/engine/verifier.py`：迁入 `_eval_condition`、`_eval_condition_inner`、`_field_value_as_text`、`_values_equal`。
   * **关键兼容要求**：`devtools/verify_field_write.py`、`verify_server_helpers.py`、`verify_verify_state.py` 会直接访问或 monkeypatch `S._field_read_width`、`S._encode_field_value`、`S._eval_condition`、`S._screen_mod`、`S.RAW_TOOLS`。因此：
     * 在 `server.py` 中 re-export 这些符号；
     * 在 `verify_state` / `wait_for` 调用 `_eval_condition` 时，仍通过 `S._eval_condition`（或传入 `S` 引用）调用，确保 `verify_verify_state.py` §6c 中 `S._eval_condition = _spy_eval` 的 monkeypatch 100% 原样生效！
2. **精简工具 docstring 的非首段冗余叙述**：
   * **铁律 1**：每个工具 docstring 的**第一段（到第一个空行为止）一字不动**！这样 `python devtools/gen_docs.py --check` 出来的 README 表格逐字节不变。
   * **铁律 2**：检查 `devtools/verify_*.py` 中对特定工具 `__doc__` 的字符串断言（如 `verify_wait_for.py` §7 要求 `"必须同时给 \`target\`" in _doc`；`verify_verify_state.py` §6b 要求 `"kind=参数" in (_RAW.__doc__ or "")`），这些关键句必须保留在 docstring 的 `Args:` 或精简要点中。
   * 将长篇幅的「为什么单独做这件事」「早期版本踩过的坑」从函数 docstring 移为函数上方的 `#` 注释（或确认已收录于 `SKILL.md`）。

#### 2.15 验收与非空转反证
1. 运行 `python devtools\gen_docs.py --check`，断言返回 0（README 表格零漂移）。
2. 运行 `python devtools\verify_tool_inventory.py`、`verify_tool_annotations.py`、`verify_cap.py`、`verify_field_write.py`、`verify_server_helpers.py`、`verify_wait_for.py`、`verify_verify_state.py`，断言全部 100% 通过。
3. 测量 `asyncio.run(S.mcp.list_tools())` 序列化后的总字节数，确认相比瘦身前（~44 KB）下降 **30% 以上**。
4. **提交信息**：`重构(server,engine): 下沉字段编码与验收求值逻辑并精简工具描述上下文开销`

### Batch 6：`hook_method` / `hook_address` 扩展受控返回值覆写 `override_retval`（P1）

#### 2.16 问题定位与取证
* **现状痛点**：目前 Agent 想验证「把 `get_Gold()` 返回值改成 `9999`」或「把 `IsUnlocked()` 返回值改成 `true`」，只能照 `SKILL.md` 配方 2 调 `eval_script` 手写 `Interceptor.attach(..., {onLeave(retval){ retval.replace(...) }})`。
* **两个严重后果**：
  1. `eval_script` 挂上去的 Hook **不在探针的 `hooks` 表里**：`hook_status()` 看不见它，`unhook()` 卸不掉它，甚至 `write_memory` 的 `_overlapping_hook` 护栏也保护不到它！
  2. Agent 手写 JS 极易在 `float`/`double` 返回值上误调 `retval.replace(...)`（其实浮点返回值在 `XMM0`，调 `retval.replace` 改的是 `RAX`，完全无效）。

#### 2.17 修改方案
遵循 `AGENTS.md §3.1` 第 3 条「不新造重复工具，用参数扩展现有工具」：
1. **探针层（`agent.js` 的 `hookAt(spec)`）**：
   * 支持 `spec.override_retval`（默认 `null`/`undefined`，表示不覆写）。
   * 新增编码辅助函数 `encodeRetvalOverride(retvalSpec, rawVal)`：
     * `bool`：`"true"/"1"` → `ptr(1)`，`"false"/"0"` → `ptr(0)`；
     * `int`/`uint`/`long`/`ulong`/`ptr`：解析整数（支持 `0x` 前缀）并转为 `NativePointer`；
     * `float`/`double`：暂若 Frida 当前 `CpuContext.xmm0` 只读不支持直接写回，则在 `hookAt` 安装前**明确拒绝并报错**（`ok: false`，提示浮点返回值覆写需改写字段或指令），绝不静默调 `retval.replace` 改 `RAX` 造成假成功！若测试确认可写或仅对整型/布尔/指针开放，则在文档与错误提示中如实声明支持范围（`int`/`uint`/`long`/`ulong`/`bool`/`ptr`）。
   * 在 `onLeave` 中：若配置了 `override_retval`，执行 `retval.replace(encodedPtr)`，并在發出的 `leave` 事件里带上 `retval_overridden: true` 与 `original_retval`（保留原返回值供核查！）。
   * 在 `hookStatus` 返回的每项中增加 `override_retval` 字段，让状态可查。
2. **会话层（`session.py`）与工具层（`server.py`）**：
   * 在 `hook_method` 与 `hook_address` 增加可选参数 `override_retval: str = ""`（默认空串 = 只观察不修改，保持原行为 100% 兼容）。
3. **五处同步**：
   * 更新 `server.py` 中 `hook_method` / `hook_address` 的签名与 docstring；
   * 运行 `python devtools\gen_docs.py --write` 同步 `README.md`；
   * 更新 `skills/gamemod-mod/SKILL.md` 配方 2（优先推荐 `hook_method(..., override_retval="9999")`，受 `unhook()` 统一管理），并同步到 `~/.dsh/skills/gamemod-mod/SKILL.md`（若本机存在该副本）。

#### 2.18 验收与非空转反证
1. **纯函数与参数校验验收（`verify_server_helpers.py`）**：
   * 断言 `override_retval="9999"` 对 `int` 返回类型编码正确；
   * 断言对 `bool` 传 `"maybe"` 或对不支持的返回类型传 `override_retval` 会直接返回 `ok: False`（不假成功）。
2. **端到端闭环验收（`verify_agent_js.py` 或 `verify_mcp_tools.py`）**：
   * 在自注入或真实游戏上对一个返回整数的函数挂 `hook_address(..., retval_type="int", override_retval="12345")`；
   * 用 `NativeFunction` 调用该函数，断言调用方拿到的返回值真变成了 `12345`，且 `drain_events()` 的 `leave` 事件同时记录了 `original_retval` 与 `retval: 12345`；
   * 调用 `unhook()` 后再次调用该函数，断言返回值**恢复为原始值**！
3. **提交信息**：`新增(runtime,server): hook_method 与 hook_address 支持受控返回值覆写 override_retval`

### Batch 7：Mono 组件实例枚举（`FindObjectsOfTypeAll`）与验收套件 `--fast` 分档（P1）

#### 2.19 问题定位与取证
1. **Mono 下 `find_instances` 命中率问题**：
   * 在 Mono 后端（`agent.js` 的 `findInstancesMono`），`mono_gc_walk_heap` 因官方限制在外部线程调用时返回 `rc=0, scanned=0`，工具如实报 `unsupported: true`。
   * 但对标 [UnityExplorer](https://www.google.com/search?q=https%3A%2F%2Fgithub.com%2Fsinai-dev%2FUnityExplorer)，Unity 游戏里 90% 需要找实例的类（`MonoBehaviour`、`Component`、`GameObject`、`ScriptableObject`、`Transform`）都继承自 `UnityEngine.Object`，而 Unity 引擎本身提供了 `UnityEngine.Resources.FindObjectsOfTypeAll(System.Type)`，在 Mono 下通过 `mono_type_get_object` + `mono_runtime_invoke` 即可直接调用！
2. **验收套件日常执行速度问题**：
   * `devtools/run_all_verify.py` 目前只能全跑或手工指定 `--only`。日常改纯函数时跑全套会拉起真实游戏，耗时长达数分钟。

#### 2.20 修改方案
1. **在 `agent.js` 的 `findInstancesMono` 中增加 `UnityEngine.Object` 派生类快路径**：
   * 先沿 `mono_class_get_parent` 向上检查目标 `klass` 是否继承自 `UnityEngine.Object`；
   * 若是，且 `mono_class_get_type`、`mono_type_get_object`、`mono_runtime_invoke` 可用（需在 `MONO_API_NAMES` 与 `MONO_SIGS` 中成对登记——**注意严格遵守 `verify_mono_runtime.py` §1 的双向一致与数值返回 `pointer` 契约，并在 4 个 Mono 样本 PE 导出表上核实存在**）：调用 `UnityEngine.Resources.FindObjectsOfTypeAll(typeObj)` 读取返回的 `MonoArray*`（长度在 `+0x18`，元素从 `+0x20` 起），填入 `matches` 并注明 `via: "Resources.FindObjectsOfTypeAll"`；
   * 若目标类**不**继承自 `UnityEngine.Object`（纯 C# 类），保持现有的 `mono_gc_walk_heap` 尝试 + `unsupported: true` 如实报错，并在错误提示中明确引导使用 `read_field_value(instance="static", ...)` 查静态单例。
2. **在 `devtools/run_all_verify.py` 中增加 `--fast`（离线快检）参数**：
   * 维护一份 `LIVE_GAME_SCRIPTS` 集合（列出需要真实 `launch_game` 启动游戏进程的脚本，如 `verify_mcp_tools.py`、`verify_mcp_extra.py`、`verify_mono_runtime.py` 等）；
   * 当传入 `--fast` 时，只运行不需要启动游戏进程的纯函数与离线解析脚本，并在结尾明确打印「`--fast` 已跳过 N 个需启动真机游戏的脚本与动态覆盖率门禁；提交完整改动前请跑不带 `--fast` 的全量门禁」（绝不拿 `--fast` 冒充全量通过）。

#### 2.21 验收与非空转反证
1. 运行 `python devtools\verify_mono_runtime.py`：
   * §1 的 `MONO_API_NAMES` == `MONO_SIGS` 双向对账必须全绿；
   * §2 的 4 个 Mono 样本 PE 导出表真值核实必须全绿（任何新登记的 `mono_*` 导出必须在 4 个样本 DLL 中 100% 存在）；
   * §3 在真机 Mono 样本上调用 `find_instances(name="Transform", namespace="UnityEngine")`，验证能否拿到非空实例列表。
2. 运行 `python devtools\run_all_verify.py --fast`，验证能在 30 秒内跑完所有离线脚本。
3. **提交信息**：`新增(runtime,devtools): Mono 实例枚举支持 Unity 对象快路径并新增 --fast 离线门禁档位`

## 3. 终局总验收清单（全部 Batch 完成后必跑）

当本地 Agent 依次完成上述批次后，必须执行以下终局验证命令序列，全部返回退出码 `0` 才算交付完成：

```powershell
# 1. 重新生成并校验 README 工具一览表与正文数字
python devtools\gen_docs.py --write
python devtools\gen_docs.py --check

# 2. 校验 10 处工具清单、已删工具标注与已安装 Skill 副本一致性
python devtools\verify_tool_inventory.py

# 3. 校验全部工具的 MCP 语义标注与三分组表
python devtools\verify_tool_annotations.py

# 4. 校验多游戏矩阵脚本的 45 工具清单对账阻断契约
python devtools\verify_runtime_matrix_inventory.py

# 5. 校验全仓 UTF-8 无 BOM 编码
pwsh -ExecutionPolicy Bypass -File .\devtools\check_encoding.ps1

# 6. 跑完整验收套件 + 动态覆盖率 --strict 门禁
python devtools\run_all_verify.py
```
