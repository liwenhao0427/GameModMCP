# GameMod MCP —— 下一阶段优化设计与执行指导书（全量源码核定版）

> **致执行 Agent 的硬性约束**：
> 1. 严格遵守 `AGENTS.md` 全部规范：UTF-8 无 BOM、不许假成功、工具改动五处同步、纯函数+端到端双层验收、**非空转反证（把代码改回旧写法确认断言报红，再还原）**[cite: 5]。
> 2. **分批独立提交**：严格按 `Batch 1` → `Batch 7` 顺序执行。每完成一个 Batch，运行 `pwsh -ExecutionPolicy Bypass -File .\devtools\check_encoding.ps1` 与对应 `verify_*.py`，确认 `git status --short` 干净后使用 `git commit -F` 提交，**严禁跨批混交 diff**[cite: 5]。

---

## Batch 1（P0 缺陷修复）：`launch_game` 丢弃 `args` 启动参数导致「说了话不干活」

### 1.1 源码定位与根因
* **`src/gamemod_mcp/server.py`（第 758～776 行）**：`launch_game` 用 `_split_argv(args)` 解析出了 `argv: list[str]`，并在返回值中回传了 `"argv": argv`，但调用 `ensure_runtime(spawn=True, launcher_exe=target, preferred_module=preferred_module)` 时**漏传了 `argv`**[cite: 3]。
* **`src/gamemod_mcp/engine/state.py`（第 608～685 行）**：`ensure_runtime` 签名缺少 `args: list[str] | None = None` 参数，且第 676 行调用 `spawn_and_attach(launcher_exe, cwd=..., preferred_module=..., init=False)` 时未传入 `args`[cite: 6]。
* **`src/gamemod_mcp/runtime/session.py`（第 705 行）**：`spawn_and_attach(exe: str, args: list[str] | None = None, ...)` **已经支持 `args` 参数**（第 717 行 `argv = [exe] + list(args or [])`）[cite: 7]，只是上层断线了。

### 1.2 具体修改步骤
1. 在 `src/gamemod_mcp/engine/state.py` 的 `ensure_runtime` 函数签名中增加 `args: list[str] | None = None`，并在第 676 行调用 `spawn_and_attach` 时传入 `args=args`[cite: 6, 7]。
2. 在 `src/gamemod_mcp/server.py` 的 `launch_game`（第 766 行）调用 `ensure_runtime` 时传入 `args=argv`[cite: 3]。

### 1.3 验收与非空转反证
1. 在 `devtools/verify_server_helpers.py`（或 `verify_spawn_stdio.py`）中新增断言：通过 monkeypatch 或桩拦截 `session.spawn_and_attach`（或 `device.spawn`），调用 `S.launch_game(exe=..., args='-popupwindow -path "C:\\My Game\\a.txt"')`，断言底层收到的 `argv` 等于 `[exe, "-popupwindow", "-path", "C:\\My Game\\a.txt"]`[cite: 3, 7]。
2. **非空转反证**：暂时去掉 `server.py` 中的 `args=argv`，运行测试确认报红；恢复后确认全绿[cite: 5]。
3. **提交**：`修复(server,state): 将 launch_game 解析后的 argv 透传至 spawn_and_attach`[cite: 5]

---

## Batch 2（P0 探针与运行时修复）：修复 `agent.js` Mono 每次 RPC 重复初始化、未摘除线程、浮点返回值退化及死函数

### 2.1 源码定位与根因
1. **`monoEnsureReady` 缺少已初始化守卫（`src/gamemod_mcp/runtime/agent.js` 第 997 行）**[cite: 7]：
   * IL2CPP 的 `ensureReady`（第 369 行）首行有 `if (attached) { return { ok: true, already: true }; }`[cite: 7]。
   * Mono 的 `monoEnsureReady`（第 997 行）在末尾第 1089 行设置了 `attached = true;`，但**函数开头没有检查 `if (attached)`**[cite: 7]！导致每次调用任何 RPC 工具（`runtimeInfo`/`classInfo`/`methodTarget`/`classFields`/`findInstances`）都会重新执行 `monoBind()`（重复创建 56 个 `NativeFunction`）、`mono_thread_attach` 和 `monoDiscoverStringLayout()`[cite: 7]。
2. **`dispose` 未摘除 Mono 线程（`agent.js` 第 1421～1427 行）**[cite: 7]：
   * `dispose` 只在 `BE.kind === 'il2cpp'` 时调用了 `releaseDomain()`，而在 Mono 模式下未调用已绑定的 `monoFuncs.mono_thread_detach` 并重置 `attached = false`[cite: 7]。
3. **Mono 方法返回值类型短名未映射（`src/gamemod_mcp/runtime/session.py` 第 111～135 行）**[cite: 7]：
   * `agent.js` 第 1265 行注明 `mono_type_get_name` 返回的是可能带短名的格式（或 `System.*`），而 `session.py` 的 `_RETVAL_SPECS` 只写了 `"System.Single"` 等长名，缺少 `"float"`, `"single"`, `"double"`, `"bool"`, `"boolean"`, `"int"`, `"int32"`, `"uint"`, `"uint32"`, `"int16"`, `"short"`, `"uint16"`, `"ushort"`, `"byte"`, `"sbyte"`, `"char"`, `"long"`, `"int64"`, `"ulong"`, `"uint64"`, `"string"` 短名映射[cite: 7]。
4. **`agent.js` 死函数 `paramNames`（第 691～701 行）**[cite: 7]：
   * `function paramNames(methodInfo)` 在 `agent.js` 中定义了但全文 0 处调用，违反 `AGENTS.md` §3「`agent.js` 中无出口的函数是死代码，应删除」（或者在 `methodsOf` 中挂载输出）[cite: 5, 7]。
5. **早期 `il2cpp_domain_get()` 抢跑触发 Boehm GC `Collecting from unknown thread` 防护（`agent.js` 第 253～264 行 & 第 406 行）**[cite: 7, 9]：
   * `il2cpp_domain_get()` 在主线程尚未执行完 `il2cpp_init` 时被 Frida 线程调用会隐式触发 `GC_init()`，使 Frida 线程被登记为主线程、真正的游戏主线程反而变成 `unknown thread`[cite: 9]。
   * 在 `domainPointer()` 中，调用 `il2cpp_domain_get()` 之前，若绑定了 `il2cpp_thread_current` 或可通过检查 `il2cpp_domain_get_assemblies` 且配合 `READY_SETTLE_DELAY` 守卫，避免在主线程初始化完成前抢先初始化 GC[cite: 7, 9]。

### 2.2 具体修改步骤
1. 在 `agent.js` 的 `monoEnsureReady`（第 997 行）开头加上：
   ```javascript
   if (attached && monoMod && monoDomain) { return { ok: true, already: true, engine: 'mono' }; }
   ```
   并在 `monoBind()` 后重置 `monoImagesCache = null; monoTypeIndexCache = null;`[cite: 7]。
2. 在 `agent.js` 中新增 `monoReleaseDomain()`（若 `monoDomain` 与 `monoFuncs.mono_thread_detach` 存在则调整状态并重置 `attached = false`），在 `dispose()` 中对 `BE.kind === 'mono'` 一并调用[cite: 7]。
3. 删除 `agent.js` 第 691～701 行未使用的死函数 `paramNames`[cite: 7]。
4. 在 `session.py` 的 `_RETVAL_SPECS`（第 111 行）中补充小写的 C# 短类型名映射（`_RETVAL_SPECS` 查找时大小写不敏感匹配或补齐短名键）[cite: 7]。

### 2.3 验收与非空转反证
1. 在 `devtools/verify_mono_runtime.py` 与 `devtools/verify_agent_js.py` 中新增断言：
   * 断言 `monoEnsureReady` 第二次调用返回 `already: True`，不会重复执行 `monoBind`[cite: 7]。
   * 断言 `_retval_spec_for("float") == "float"`、`_retval_spec_for("int") == "int"`[cite: 7]。
   * 断言 `agent.js` 中不存在未被引用的局部 `function xxx(` 死函数[cite: 7]。
2. **非空转反证**：去掉 `monoEnsureReady` 的 `if (attached)` 守卫，断言报红；还原后通过[cite: 5]。
3. **提交**：`修复(runtime): 补齐 monoEnsureReady 幂等守卫、返回值短名映射并清理探针死代码`[cite: 5]

---

## Batch 3（P0 缺陷修复）：枚举（Enum）字段在 `read_field_value` 误读 8 字节、在 `set_field_value` 被拒

### 3.1 源码定位与根因
* **`src/gamemod_mcp/server.py`（第 295～360 行、第 990～1160 行）**[cite: 3]：
  * `read_field_value` docstring 声明支持 `int/float/bool/enum 等`[cite: 3]。
  * 但 `agent.js` 的 `fieldsOf`（第 614 行）和 `monoFieldsOfOwn`（第 1277 行）只返回字段的 `type` 字符串（枚举字段返回的是枚举类名，如 `"EasingType"`），未标记该字段类型是否为枚举[cite: 7]。
  * 导致 `server.py` 的 `_field_read_width` 查不到 `_FIELD_WIDTH` 表，对 4 字节枚举默认读 **8 字节**（把相邻字段的 4 字节拼进来，违反 `AGENTS.md` §4）[cite: 3, 5]；而 `set_field_value`（第 1126 行）因 `width == 8` 且类型名不在 `_FIELD_WRITE_KIND` 中，将其误判为**「疑似引用类型」直接报错拒绝写入**[cite: 3]！

### 3.2 具体修改步骤
1. **探针层补充字段枚举判定（`src/gamemod_mcp/runtime/agent.js`）**[cite: 7]：
   * 在 `API_NAMES` 与 `MONO_API_NAMES` 中确认/补充 `il2cpp_class_from_il2cpp_type`（或 `il2cpp_class_from_type`）与 `mono_class_from_mono_type`（若存在导出），用于在 `fieldsOf` / `monoFieldsOfOwn` 中拿到字段类型的 `klass` 并调 `il2cpp_class_is_enum` / `mono_class_is_enum` 标记 `is_enum: true`[cite: 7]。
2. **索引层与工具层兜底（`src/gamemod_mcp/server.py`）**[cite: 3]：
   * 在 `server.py` 新增辅助函数 `_resolve_enum_underlying(st, ftype: str, hit: dict) -> str | None`：
     * 若 `hit.get("is_enum")` 为 True，返回 `hit.get("enum_base_type") or "System.Int32"`；
     * 若 `st.has_index()` 且 `ftype` 不在 `_FIELD_WIDTH` 中，查当前 SQLite 索引 `SELECT is_enum FROM types WHERE nested_name = ? OR full_name = ? OR name = ?`：若确认是枚举，按 `"System.Int32"`（4 字节 `int`）处理[cite: 3, 6]！
   * 在 `read_field_value` 与 `set_field_value` 中用解析后的底层类型计算 `width` 与 `kind`[cite: 3]。

### 3.3 验收与非空转反证
1. 在 `devtools/verify_field_write.py` 中新增纯函数 + 端到端枚举字段读写测试：断言枚举字段 `read_width == 4`，且 `set_field_value` 能成功写入整数并回读 `verified == True`[cite: 5, 9]。
2. **非空转反证**：注释掉枚举判定逻辑，断言 `read_field_value` 退回 8 字节且 `set_field_value` 报「疑似引用类型」失败；还原后全绿[cite: 3, 5]。
3. **提交**：`修复(server,runtime): 识别枚举字段底层宽度，修复枚举误读 8 字节与写入被拒`[cite: 5]

---

## Batch 4（P1 缺陷修复）：`verify_state` 与 `wait_for` 的 5 处逻辑与文案缺陷

### 4.1 源码定位与根因
* **定位**：`src/gamemod_mcp/server.py`（第 1085～1375 行）[cite: 3]。
* **5 处具体问题**：
  1. **语法报错提示写反**：`verify_state` 第 1301 行按 `if "=" not in raw:` 校验，但第 1303 行报错写 `"写法不对：应是 kind:参数"`（docstring 第 1255 行也写成 `kind:参数`），诱导 Agent 传冒号陷入死循环[cite: 3]。
  2. **`wait_for` docstring 参数名写错**：第 1197 行写 `必须同时给 path`，实际参数名叫 `target`[cite: 3]。
  3. **嵌套类名被 `rsplit(".", 1)` 拆坏**：`verify_state` 第 1327、1338 行用 `ns, short = cls.rsplit(".", 1)` 把 `Outer.Inner` 拆成 `namespace="Outer", name="Inner"`，违反 `AGENTS.md` 与 `SKILL.md`「嵌套类型的 namespace 为空，必须把完整嵌套名传给 `name`」的规则[cite: 3, 5, 8]。
  4. **`independent_count` 未校验通道独立性**：第 1349 行直接令 `independent_count = passed`，导致传入两条同为 `ocr=` 的断言（如 `"ocr=精核,ocr=108"`）也会被判为满足 2 条独立证据[cite: 3]。
  5. **`_eval_condition_inner` 的 `field` 分支盲取 `ptrs[0]`**：未优先筛选 `class_name` 精确匹配的实例，且只读第 0 个实例，遇到派生类混入时易假阴性[cite: 3, 7]。

### 4.2 具体修改步骤
1. 将 `verify_state` docstring 第 1255 行与第 1303 行报错提示中的 `kind:参数` 修正为 `kind=参数`[cite: 3]。
2. 将 `wait_for` docstring 第 1197 行的 `必须同时给 path` 修正为 `必须同时给 target`[cite: 3]。
3. 在 `verify_state` 解析 `field` 和 `instances` 分支时，直接传 `name=cls.strip(), namespace=""`（交由探针的 `resolveClass` 统一处理顶层类与嵌套类完整名）[cite: 3, 7]。
4. 在 `verify_state` 中，将 `independent_count` 定义为**命中断言的不同信号源种类数**（如 `len({r["kind"] for r in results if r["hit"]})`），当 `passed >= require` 但 `independent_count < require` 时，判 `verdict = "fail"` 并说明「虽然成立 N 条，但均来自同一通道（缺独立第 2 通道，如日志 + OCR/字段）」[cite: 3, 8]。
5. 在 `_eval_condition_inner` 的 `kind == "field"` 分支中，将 `class_name == short_name` 的精确匹配实例排在前面，并遍历 `ptrs[:8]`，只要有一个实例字段命中 `equals` 即判 `hit = True`[cite: 3, 7]。

### 4.3 验收与非空转反证
1. 更新 `devtools/verify_verify_state.py` 与 `devtools/verify_wait_for.py`：增加嵌套类名解析断言、同通道双 `ocr=` 拒绝提升为 `pass` 的断言、非第 0 个实例命中 `field` 的断言[cite: 5]。
2. **非空转反证**：把 `independent_count` 改回 `passed`，确认双 `ocr=` 负向断言报红；还原后全绿[cite: 5]。
3. **提交**：`修复(server): 修正 verify_state 与 wait_for 的嵌套类名解析、独立证据通道判定及文案`[cite: 5]

---

## Batch 5（P1 门禁、并发锁与文档对账）：落实《交接与评审说明书》§11 全部未决项

### 5.1 源码定位与修改清单
1. **彻底修复 `devtools/report_runtime_matrix.py` 漏掉 7 个工具及不一致静默放行（§11.1）**[cite: 4, 9]：
   * **联动点 1（第 86 行 `ALL_TOOLS`）**：补齐 `find_instances`, `set_field_value`, `capture_screen`, `ocr`, `probe_log`, `wait_for`, `verify_state` 共 45 个工具[cite: 4, 9]。
   * **联动点 2（第 115 行 `RUNTIME_TOOLS`）**：将 `find_instances` 与 `set_field_value` 加入 `RUNTIME_TOOLS`（以便 UE 游戏正确将它们标记为 `skip`）[cite: 9]。
   * **联动点 3（第 644～666 行 `KEY_STATIC` / `KEY_RUNTIME`）**：将 7 个工具补入对应 Markdown 表格列清单[cite: 9]。
   * **联动点 4（`Probe` 执行阶段）**：在 `stage_this` / `stage_memory` 中加入对 `find_instances`、`set_field_value` 及弱视觉/验收工具的实测或按条件 `skip` 调用，杜绝出现 `⬜（没跑到）`[cite: 9]。
   * **联动点 5（第 584 行清单对账阻断）**：当 `out["tools_missing"]` 或 `out["tools_extra"]` 非空时，打印错误并立即 `return 1`（或 `sys.exit(1)`），**绝不带病继续跑或生成假报告**[cite: 4, 9]！
2. **为 `ResultStore` 补齐线程锁并扩容 LRU（回应 §11.3 及线程池并发安全）**[cite: 4, 6]：
   * 在 `src/gamemod_mcp/engine/index.py` 的 `ResultStore.__init__`（第 307 行）中：
     * 将默认容量 `max_entries: int = 32` 调大为 **`128`**，防止单次会话内多次搜索挤掉旧句柄[cite: 4, 6]；
     * 增加 `self._lock = threading.Lock()`，在 `put()`、`get()`、`handles()`、`clear()` 中加锁保护 `self._items` 字典与 `self._counter`，消除 `asyncio.to_thread` 下的多线程字典并发竞态[cite: 3, 6]。
   * 保持 `search_classes` 等 5 个工具的 `readOnlyHint=True` 不变[cite: 4, 9]。
3. **微调 `drain_events` 与 `gc_control` 的语义标注（回应 §11.2 / §11.4 / §11.6）**[cite: 4, 9]：
   * **`drain_events`（§11.2）**：在 `server.py` 第 940 行改为 `destroy=False`（保留 `read_only=False, idempotent=False, open_world=True`），使其不再与 `eval_script` / `write_memory` 混为一谈触发客户端高危弹窗[cite: 3, 4]。
   * **`gc_control`（§11.4）**：在 `server.py` 第 1030 行改为 `idempotent=False`[cite: 3, 4]；并在 `agent.js` 的 `dispose()` 中，若 `gcDisabled === true` 且 `funcs.il2cpp_gc_enable` 存在，自动恢复 GC 防止泄漏[cite: 7]。
   * **同步更新 `devtools/verify_tool_annotations.py`（§11.6）**：**保留三张手工分组表**（防恒真自证循环），在 `MUTATING` 和 `MUST_BE_DESTRUCTIVE` 中同步更新 `drain_events` 与 `gc_control` 的期望元组，并将第 154 行等处的硬编码文案 `"38 个工具"` 改为 `f"{len(tools)} 个工具"`（同步清理 `verify_cap.py`、`verify_mcp_context.py` 的旧数字文案，回应 §11.5）[cite: 4, 9]。
4. **修复 `il2cpp_meta.py` 与 `pe.py` 的两处小缺陷**[cite: 3]：
   * `src/gamemod_mcp/engine/il2cpp_meta.py` 第 1345 行：`Metadata.search_types` 中将 `name = t.full_name` 改为 `name = t.nested_name or t.full_name`（与 `mono_meta.py` 第 1166 行的 `search_types` 保持一致）[cite: 3, 8]。
   * `src/gamemod_mcp/engine/pe.py` 第 389 行：删除 `_parse_delay_imports` 中未使用的错误行 `int_rva = info.rva_to_offset(base)`[cite: 3]。
5. **订正 `README.md` 与 `docs/交接与评审说明书.md` 中关于事件循环的过时描述**[cite: 4, 5, 9]：
   * `README.md` 第 525～545 行与 `docs/交接与评审说明书.md` §6.2 / §10.2 仍写着「同步工具内联调用占住事件循环，60 秒 `launch_game` 期间服务器什么都干不了」[cite: 4, 5]；但实际上 `server.py` 的 `_install_event_loop_guard` 早已将同步工具 offload 到线程池，且《重启验证报告-第二轮》§4 已实测慢调用期间 `get_session` 10/10 秒回[cite: 3, 9]。请将这两处文档订正为当前真实实现（保留「不做 `notifications/progress`」的结论，但删除「事件循环被占满」的过时说法）[cite: 4, 5, 9]。

### 5.2 验收与非空转反证
1. 运行 `python devtools/verify_tool_annotations.py`、`python devtools/verify_tool_inventory.py`、`python devtools/verify_parser.py` 全部通过[cite: 9]。
2. **非空转反证**：临时从 `report_runtime_matrix.py` 的 `ALL_TOOLS` 中删掉 1 个工具，运行 `python devtools/report_runtime_matrix.py --only NonExistent`（或触发对账分支），确认其非零退出报错；还原后通过[cite: 4, 9]。
3. **提交**：`修复(devtools,engine): 补齐运行时矩阵 45 工具对账阻断、ResultStore 线程锁与语义标注`[cite: 5]

---

## Batch 6（P2 性能与上下文减负）：`cap.py` 截断收敛加速 + 工具 Schema Token 瘦身

### 6.1 具体修改步骤
1. **`src/gamemod_mcp/cap.py` 序列化收敛优化与多列表句柄保护**[cite: 3]：
   * 在 `_fit(payload, key, items, cur)`（第 70～101 行）中，既然已算出 `other = measure(base)`，在收敛循环中用 `other + measure(items[:keep])` 代替每次整包 `measure(probe)`，跳出循环后再做 1 次整包校验，减少约 60% 的大 JSON 重复序列化开销[cite: 3]。
   * 在 `_register(out, key, items, store)`（第 104～115 行）中，若 `"handle" in out` 且不是复用原句柄，额外写一份 `out[f"{key}_handle"] = handle`，防止单次返回含两个超限列表时前一个句柄被覆盖[cite: 3]。
2. **精简 `server.py` 工具 docstring 非首段冗余文本（回应 §10.4：降低 44 KB / 1.3 万 Token 固定成本）**[cite: 3, 4, 9]：
   * **铁律 1**：**绝对不改动任何工具 docstring 的第一段**（`devtools/gen_docs.py` 的 `first_paragraph` 依赖首段生成 README 工具表）[cite: 9]。
   * **铁律 2**：若修改了 `skills/gamemod-mod/SKILL.md`，**必须同时注意**：
     * 新增的任何带 `_` 的反引号非工具词必须同步加入 `devtools/verify_tool_inventory.py` 的 `NON_TOOL_IDENTIFIERS`（第 61 行）[cite: 9]；
     * 必须将 `skills/gamemod-mod/SKILL.md` 字节级同步复制到 `~/.dsh/skills/gamemod-mod/SKILL.md`（若该目录存在），否则 `verify_tool_inventory.py` 第 9 节必报红[cite: 9]！
   * 精简 `server.py` 各工具 docstring 第 2 段及以后的重复历史故事（那些内容已在 `SKILL.md` 中完整存在），保留「首段摘要 + 核心前置/拒绝条件 + Args + Returns」[cite: 3, 8]。

### 6.2 验收与非空转反证
1. 运行 `python devtools/gen_docs.py --check`、`python devtools/verify_cap.py`、`python devtools/verify_response_size.py`、`python devtools/verify_tool_inventory.py` 确认全部通过[cite: 5, 9]。
2. 测量 `await S.mcp.list_tools()` 序列化后的总字节数，记录优化前后的 Token/字节缩减比例[cite: 4]。
3. **提交**：`优化(cap,server): 优化响应上限收敛测量并精简工具 docstring 降低固定上下文开销`[cite: 5]

---

## Batch 7（P2 高杠杆能力增强）：`read_field_value` / `set_field_value` 支持静态字段读写（打通 Mono 拿单例指针闭环）

### 7.1 痛点与设计依据
* 在 Mono 游戏中，`find_instances` 因 Mono 官方 `mono_gc_walk_heap` 限制会如实返回 `unsupported: true`[cite: 7, 8, 9]。此时 Agent 想拿单例对象（如 `GameManager.Instance` / `PlayerController.s_Instance`）的指针，除了挂 Hook 守株待兔外，最直接的正路是**读取静态字段的值**。
* 同时，《最后一公里验收-调研与思路.md》§2.1 指出：`agent.js` 中早已绑定了 `il2cpp_field_static_get_value` 和 `mono_field_static_get_value`，但在探针内的调用次数为 **0**（属于已绑定未接线的能力）[cite: 5, 7]。
* 按 `AGENTS.md` §3.1 第 3 条「一个能力只暴露一个工具，用参数区分」[cite: 5]，无需新增工具，直接让 `read_field_value` 与 `set_field_value` 支持 `instance="static"`（或 `instance=""`）即可完美闭环！

### 7.2 具体修改步骤
1. **在 `src/gamemod_mcp/runtime/agent.js` 中补齐静态字段读写接口**[cite: 7]：
   * 在 `BE_IL2CPP` 与 `BE_MONO`（以及 `rpc.exports`）中增加 `readStaticField(opts, preferredModule)`（或扩展 `classFields`）：
     * 对 IL2CPP：定位静态字段 `f`，分配 8 字节缓冲 `buf = Memory.alloc(8)`，调用 `funcs.il2cpp_field_static_get_value(f, buf)` 读取字节并返回 hex[cite: 7]。
     * 对 Mono：定位静态字段 `f`，通过 `monoFuncs.mono_class_vtable(monoDomain, klass)`（若已绑定）或 `mono_field_static_get_value` 读取静态字段值[cite: 7]。
     * （注：若新增了 `rpc.exports` 导出方法，必须在 `session.py` 的 `RuntimeSession` 和 `server.py` 的 `read_field_value` 中调用它，确保 `verify_agent_js.py` 门禁通过）[cite: 7, 9]。
2. **在 `src/gamemod_mcp/server.py` 的 `read_field_value` 中支持 `instance="static"`**[cite: 3]：
   * 当 `instance.strip().lower() in ("static", "")` 时，查询 `static_only=True` 的字段，调用探针读取静态字段字节并按 `_field_read_width` 与 `_explain_bytes` 解释返回（若是引用类型静态字段，其 `as_pointer` 即为单例对象的实例指针，可直接喂给后续的实例字段 `read_field_value` / `set_field_value`！）[cite: 3]。
3. **同步更新五处文档**（`server.py` docstring、`python devtools/gen_docs.py --write`、`skills/gamemod-mod/SKILL.md` 及 `~/.dsh/skills/` 副本、验收脚本）[cite: 5, 9]。

### 7.3 验收与非空转反证
1. 运行 `python devtools/verify_agent_js.py`、`python devtools/verify_field_write.py`、`python devtools/verify_tool_inventory.py` 及全套 `python devtools/run_all_verify.py`[cite: 9]。
2. 确保全套验收脚本 **100% 通过**，且 `report_tool_coverage.py --strict` 动态覆盖率门禁（45/45）全绿[cite: 9]。
3. **提交**：`新增(runtime,server): read_field_value 支持静态字段读取，打通静态单例指针获取路径`[cite: 5]