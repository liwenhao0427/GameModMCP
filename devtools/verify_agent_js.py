# Frida 探针脚本的静态检查
#
# 用法：
#   1) 语法检查：node --check 会在 Frida 专有的全局（Process/Interceptor/send/
#      rpc）上报 "not defined"，那是预期的——node 只负责抓真正的语法错误。
#   2) 真实加载检查：把脚本注入本进程的 frida，用 rpc.exports.ping 验证
#      Frida 能编译并执行它。这是最有力的检查，因为 Frida 的 JS 引擎
#      （QuickJS/V8）与 node 并不相同。
import sys, io, json, re, inspect, subprocess, pathlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
AGENT = ROOT / "src" / "gamemod_mcp" / "runtime" / "agent.js"
sys.path.insert(0, str(ROOT / "src"))

# ---- 断言计数（与其它 verify_*.py 一致：结尾汇总 + 非零退出码）
PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(label)
    print(f"  {'✅' if cond else '❌'} {label}"
          + (f" — {detail}" if detail else ""))
    return cond


print("=" * 74)
print("Frida 探针静态检查")
print("=" * 74)
print(f"  脚本: {AGENT}")
print(f"  大小: {AGENT.stat().st_size:,} 字节")

src = AGENT.read_text(encoding="utf-8")
print(f"  编码: UTF-8  行数: {src.count(chr(10)) + 1:,}")
# BOM 检查
raw = AGENT.read_bytes()
# 表达式提到 f-string 外面：3.12 之前 f-string 的 {} 里不许出现反斜杠。
# 这处与下面那处是同一个毛病 —— 修掉下面那处之后，脚本才第一次真正
# 编译到这里，于是这处**紧跟着**暴露出来。两处都是「声明支持 3.10 却用了
# 3.12 语法」，所以一起改。
_has_bom = raw[:3] == b"\xef\xbb\xbf"
print(f"  BOM : {'存在（不符合要求）' if _has_bom else '无（符合要求）'}")

# ---------- node 语法检查
print()
print("-" * 74)
print("node --check（只抓语法错误；Frida 全局变量未定义属预期）")
print("-" * 74)
r = subprocess.run(["node", "--check", str(AGENT)],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", shell=False)
if r.returncode == 0:
    print("  ✅ 语法通过")
else:
    print(f"  ❌ 语法错误 (exit {r.returncode})")
    print(r.stdout or "")
    print(r.stderr or "")
    sys.exit(1)

# ---------- 真实 Frida 加载检查
print()
print("-" * 74)
print("Frida 真实加载（把脚本注入本进程，验证能被 Frida 编译执行）")
print("-" * 74)
try:
    import frida
except ImportError as e:
    print(f"  ⚠️  frida 未安装，跳过真实加载检查：{e}")
    sys.exit(0)

device = frida.get_local_device()
try:
    session = device.attach("gamemod-selftest")
except Exception:
    session = device.attach(__import__("os").getpid())

try:
    script = session.create_script(src)
    msgs = []
    script.on("message", lambda m, d: msgs.append(m))
    script.load()
    print("  ✅ 脚本被 Frida 成功编译并加载")

    try:
        ping = script.exports_sync.ping()
        print(f"  ✅ rpc.exports.ping 可用：{json.dumps(ping, ensure_ascii=False)}")
    except Exception as e:
        print(f"  ❌ rpc.exports.ping 调用失败：{e}")

    exports = [m for m in msgs if m.get("type") == "send"]
    if exports:
        # 表达式必须提到 f-string 外面：`pyproject.toml` 声明 requires-python
        # >=3.10，而 3.12 之前 f-string 的 {} 里不允许跨行/反斜杠。
        _payload = json.dumps(exports[0].get("payload"), ensure_ascii=False)
        print(f"  探针加载期消息：{_payload}")
    errors = [m for m in msgs if m.get("type") == "error"]
    if errors:
        print("  ⚠️  加载期错误：")
        for e in errors:
            print(f"     {json.dumps(e, ensure_ascii=False)}")

    # 列出脚本导出的接口，确认工具面完整
    try:
        print()
        print("  脚本导出的 RPC 接口（即宿主可调用的运行时能力）：")
        for name in sorted(ping.keys()):
            pass
    except Exception:
        pass

    # 契约 §3：agent.js 里的能力必须有 MCP 出口；没有出口的函数是死代码。
    # 这条断言把「删掉/接上」这件事钉死，避免又长出一批没人调的接口。
    probed = None
    _sess_src = (ROOT / "src" / "gamemod_mcp" / "runtime" / "session.py"
                 ).read_text(encoding="utf-8")
    _agent_src = (ROOT / "src" / "gamemod_mcp" / "runtime" / "agent.js"
                  ).read_text(encoding="utf-8")
    # 只看 rpc.exports 块内的接口 —— onEnter/onLeave 是 Frida Hook 的回调，
    # 不是宿主可调用的 RPC 接口，不该混进来。
    _block = _agent_src.split("rpc.exports")[-1]
    _ifaces = re.findall(r"^\s*(\w+)\s*:\s*function", _block, re.M)

    def _snake(n):
        return re.sub(r"(?<!^)(?=[A-Z])", "_", n).lower()

    # 已知「有出口」或「被验证脚本使用」的例外（都是有意保留的能力）
    _allowed = {"ping", "describeAddress", "fail", "send"}
    _orphan = []
    for n in _ifaces:
        if n in _allowed:
            continue
        if re.search(rf"\b{n}\b", _sess_src) or re.search(rf"\b{_snake(n)}\b",
                                                          _sess_src):
            continue
        _orphan.append(n)
    print(f"  探针接口共 {len(_ifaces)} 个，其中无宿主出口的: "
          f"{_orphan if _orphan else '无'}")
    check("探针 RPC 接口全部有宿主侧出口（无死代码）", not _orphan,
          f"无出口={_orphan}")

    # 探针 console.log 必须能被 drain_events 取到（以前只写进一个没人读的列表）
    import gamemod_mcp.runtime.session as _rs
    _onmsg = inspect.getsource(_rs.RuntimeSession._on_message)
    check("探针 log 消息已入事件队列（drain_events 可取到）",
          'mtype == "log"' in _onmsg and "_push_event" in _onmsg,
          "原先只 append 到一个无人读取的 _console 列表，探针日志写进去就消失")
    _sess_full = (ROOT / "src" / "gamemod_mcp" / "runtime" / "session.py"
                  ).read_text(encoding="utf-8")
    # 只写不读的 _console 必须已经清掉（否则又是一条死管道）。
    # 注意要**先剥掉注释**再判：修复说明里会提到这些名字，
    # 按原文匹配会把「解释为什么删」误判成「还没删」。
    def _strip_comments(t: str, marker: str) -> str:
        out = []
        for ln in t.splitlines():
            s = ln.lstrip()
            if s.startswith(marker) or s.startswith('"""') or s.startswith("'''"):
                continue
            if marker in ln:
                ln = ln.split(marker, 1)[0]
            out.append(ln)
        return "\n".join(out)

    _sess_code = _strip_comments(_sess_full, "#")
    check("只写不读的 _console / console_output 已从代码中清理",
          "_console" not in _sess_code and "console_output" not in _sess_code,
          "（已剥除注释后判定）")
    # backtraceHere 与 Hook 内联的回溯重复，已删除
    _agent_full = (ROOT / "src" / "gamemod_mcp" / "runtime" / "agent.js"
                   ).read_text(encoding="utf-8")
    check("agent.js 里与 Hook 内联重复的 backtraceHere 已删除",
          "backtraceHere" not in _agent_full)

    # ---- Mono 侧初始化幂等 + 卸载摘线程（与 IL2CPP 侧对称）----
    # 这两条都是「另一个后端有、Mono 后端漏了」的对称性缺陷：
    #   · IL2CPP 的 ensureReady 首行有 `if (attached) return already`，
    #     Mono 的 monoEnsureReady 没有 → 每次 RPC 都重跑 monoBind()（重建
    #     56 个 NativeFunction）、重复 attach、重复探字符串布局。
    #   · IL2CPP 卸载前调 releaseDomain()，Mono 侧从不调 mono_thread_detach
    #     → 域里留悬空线程记录（IL2CPP 那条正是 0xc0000005 崩溃的根因）。
    def _fn_body(name: str, src_text: str) -> str:
        """粗取一个函数的函数体（按大括号配平）。

        两种写法都要认：`function foo(...) {...}`（普通函数）与
        `foo: function (...) {...}`（挂在 `rpc.exports` / 后端对象上的方法）。
        """
        m = re.search(rf"(?:function\s+{re.escape(name)}\s*\(|\b{re.escape(name)}\s*:\s*function\s*\()",
                      src_text)
        if not m:
            return ""
        i = src_text.find("{", m.end())
        if i < 0:
            return ""
        depth, j = 0, i
        while j < len(src_text):
            if src_text[j] == "{":
                depth += 1
            elif src_text[j] == "}":
                depth -= 1
                if depth == 0:
                    return src_text[i:j + 1]
            j += 1
        return src_text[i:]

    _mono_ready = _fn_body("monoEnsureReady", _agent_full)
    _il2cpp_ready = _fn_body("ensureReady", _agent_full)
    check("取到了两个后端的 ensureReady 函数体（断言不是空转）",
          bool(_mono_ready) and bool(_il2cpp_ready),
          f"mono={len(_mono_ready)} 字节 il2cpp={len(_il2cpp_ready)} 字节")
    check("Mono 的 monoEnsureReady 有 `attached` 幂等守卫",
          "if (attached" in _mono_ready,
          "没有它每次 RPC 都会重跑 monoBind()")
    check("两个后端的幂等守卫写法一致（对称）",
          "if (attached" in _il2cpp_ready and "if (attached" in _mono_ready,
          "IL2CPP 一直有，Mono 之前漏了")

    check("agent.js 定义了 monoReleaseDomain（卸载前摘 Mono 线程）",
          re.search(r"function\s+monoReleaseDomain\s*\(", _agent_full) is not None)
    _mono_rel = _fn_body("monoReleaseDomain", _agent_full)
    check("monoReleaseDomain 调用了 mono_thread_detach",
          "mono_thread_detach" in _mono_rel,
          "只定义了不用等于没修")
    _dispose = _fn_body("dispose", _agent_full)
    check("取到了 dispose 的函数体（断言不是空转）", bool(_dispose),
          f"{len(_dispose)} 字节")
    check("dispose 对 Mono 后端也调用了摘域",
          "monoReleaseDomain" in _dispose,
          "原来只在 BE.kind === 'il2cpp' 时调 releaseDomain()")
    check("dispose 会恢复被暂停的 GC（防止卸载后内存持续增长）",
          "gcDisabled" in _dispose and "gcControl" in _dispose,
          "走 BE.gcControl(false) 而不是直接调 il2cpp_*（引擎差异只在后端里）")
    check("dispose 里恢复 GC 也走后端对象（不直连 IL2CPP 导出）",
          "funcs.il2cpp_" not in _dispose,
          "RPC 层直接调 il2cpp_* 是架构违规，verify_mono_runtime 会报红")

    # ---- 死代码：定义了但全文没有其它引用点的局部 function ----
    # 契约 §3：agent.js 里的能力必须有 MCP 出口；没有出口的函数是死代码。
    # `paramNames` 就是这样一个函数（定义了、从没被调用过），已删除。
    # 判定方式：把该函数的**声明**逐个抹掉，再看标识符是否还在别处出现。
    # 这样引用点写成 `foo(...)`、`BE.foo`、`x: foo` 都能算到。
    _decls = re.findall(r"^\s*function\s+(\w+)\s*\(", _agent_full, re.M)
    _dead = []
    for _n in sorted(set(_decls)):
        _rest = re.sub(rf"function\s+{re.escape(_n)}\s*\(", "function __decl__(",
                       _agent_full)
        if not re.search(rf"\b{re.escape(_n)}\b", _rest):
            _dead.append(_n)
    check("agent.js 没有「定义了但从不引用」的局部函数",
          not _dead, f"死函数={_dead}" if _dead else
          f"{len(set(_decls))} 个局部函数都有引用点")
    check("反证：paramNames 确实已不在 agent.js 里（它原来是死函数）",
          not re.search(r"function\s+paramNames\s*\(", _agent_full),
          "定义了但全文 0 处调用")

    # ================================================================
    # Batch 1：Windows x64 混合参数的寄存器槽位（ABI 缺陷回归）
    # ================================================================
    #
    # 为什么必须有这一节：Windows x64 的前 4 个参数**按位置共享编号**
    # （第 i 个参数占第 i 号槽：RCX/XMM0、RDX/XMM1、R8/XMM2、R9/XMM3），
    # 而 System V（Linux/macOS）是整型与浮点**各自独立编号**。
    # 旧代码对两种 ABI 都套用了 System V 的「双计数器」写法，
    # 于是在 Windows 上从第 2 个参数起整型与浮点**全部读错槽位**。
    #
    # 实测真值（MSVC /O2 编译的真 DLL + MSVC 编译的调用方，
    # `mixed(void*, float, int)` 传 (0x1000, 2.5, 42)）：
    #     真值   [0x1000, 2.5, 42]
    #     旧逻辑 [0x1000, 194415165440, 0x1000]   ← 后两个全错，且不报错
    #     新逻辑 [0x1000, 2.5, 42]
    print()
    print("-" * 74)
    print("Batch 1：Windows x64 混合参数寄存器槽位（ABI 回归）")
    print("-" * 74)

    _slot_src = _fn_body("argSlotIndexes", _agent_full)
    check("agent.js 定义了 argSlotIndexes（槽位计算抽成纯函数）",
          bool(_slot_src), f"{len(_slot_src)} 字节")
    # 断言要**收窄到 onEnter 里**：argSlotIndexes 自己的 System V 分支里
    # 仍然合法地写着 `var intIdx = 0, fpIdx = 0`（那是另一种 ABI 的正确写法），
    # 全文匹配会把它误判成「还没修」。
    _hook_at = _fn_body("hookAt", _agent_full)
    check("取到了 hookAt 的函数体（断言不是空转）", bool(_hook_at),
          f"{len(_hook_at)} 字节")
    check("反证：onEnter 里的旧「双计数器」写法已移除",
          "var intIdx = 0, fpIdx = 0" not in _hook_at,
          "它对 Windows x64 是错的（System V 才各自编号）")
    check("onEnter 用 argSlotIndexes 算槽位（不只是定义了没用）",
          "argSlotIndexes(" in _hook_at,
          f"hookAt 里出现 {_hook_at.count('argSlotIndexes(')} 次")
    check("第 5 个及以后的浮点走栈参数（不拿 xmm4/5 冒充）",
          "stackArgFloat(" in _hook_at and "slotI < 4" in _hook_at,
          "xmm4/xmm5 实测恒为 0")

    # ---- 纯函数行为：把 agent.js 里**真实的那段源码**取出来跑，不是抄一份
    #
    # 注意 `eval_js` 是在 `new Function(...)` 里求值的**新作用域**，
    # 看不到探针 IIFE 的闭包变量 —— 所以这里用取出的函数体**重建**一个函数，
    # 而不是直接 `eval_js("argSlotIndexes(...)")`（那样只会得到 ReferenceError）。
    _slot_value = None
    try:
        _code = (
            "(function(){"
            " var argSlotIndexes = function (argSpecs, platform, arch) "
            + _slot_src + ";"
            " return {"
            "  win_ptr_float_int: argSlotIndexes(['ptr','float','int'],"
            "                                    'windows','x64'),"
            "  win_int_float: argSlotIndexes(['int','float'], 'windows','x64'),"
            "  win_float_int: argSlotIndexes(['float','int'], 'windows','x64'),"
            "  win_five: argSlotIndexes(['ptr','float','int','float','int'],"
            "                           'windows','x64'),"
            "  sysv_ptr_float_int: argSlotIndexes(['ptr','float','int'],"
            "                                     'linux','x64')"
            " }; })()"
        )
        _r = script.exports_sync.eval_js(_code)
        if isinstance(_r, dict) and not _r.get("ok"):
            print(f"  ⚠️  eval_js 报错：{_r.get('error')}")
        _slot_value = _r.get("value") if isinstance(_r, dict) else None
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ⚠️  eval_js 失败：{exc}")

    check("取到了 argSlotIndexes 的实测返回值（断言不是空转）",
          isinstance(_slot_value, dict), f"{_slot_value}")
    if isinstance(_slot_value, dict):
        check("Windows x64：槽位下标 == 参数位置（ptr,float,int → [0,1,2]）",
              _slot_value.get("win_ptr_float_int") == [0, 1, 2],
              f"{_slot_value.get('win_ptr_float_int')}")
        check("Windows x64：(int,float) 的 float 在槽 1（不是 XMM0）",
              _slot_value.get("win_int_float") == [0, 1],
              f"{_slot_value.get('win_int_float')}")
        check("Windows x64：(float,int) 的 int 在槽 1（不是 RCX）",
              _slot_value.get("win_float_int") == [0, 1],
              f"{_slot_value.get('win_float_int')}")
        check("Windows x64：第 5 个参数仍在槽 4（栈参数照样有槽号）",
              _slot_value.get("win_five") == [0, 1, 2, 3, 4],
              f"{_slot_value.get('win_five')}")
        check("System V 仍是整型/浮点各自编号（ptr,float,int → [0,0,1]）",
              _slot_value.get("sysv_ptr_float_int") == [0, 0, 1],
              f"{_slot_value.get('sysv_ptr_float_int')}")

    # ---- 端到端：造一个 (void*, float, int) 真函数，走探针自己的 hookAt 路径
    _C_SRC = ("int gm_abi(void* p, float f, int n) { return n + (int)f; }")
    _cm_code = (
        "(function(){ var cm = new CModule(" + json.dumps(_C_SRC) + ");"
        " globalThis.__gm_abi_cm = cm;"      # 保引用：CModule 被 GC 后调用会崩
        " return cm.gm_abi.toString(); })()"
    )
    _abi_addr = None
    try:
        _r2 = script.exports_sync.eval_js(_cm_code)
        _abi_addr = _r2.get("value") if isinstance(_r2, dict) else None
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ⚠️  CModule 创建失败：{exc}")

    check("造出了混合签名的原生靶子（(void*, float, int)）",
          bool(_abi_addr), f"addr={_abi_addr}")

    _enters = []
    if _abi_addr:
        try:
            script.exports_sync.hook({
                "address": _abi_addr, "args": ["ptr", "float", "int"],
                "retval_type": "int", "label": "abi-slot-test",
                "max_calls": 10,
            })
            script.exports_sync.eval_js(
                "(function(){ var f = new NativeFunction(ptr("
                + json.dumps(_abi_addr) + "), 'int',"
                " ['pointer','float','int']); return f(ptr('0x1000'), 2.5, 42); })()")
            import time as _time
            _time.sleep(0.4)
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️  Hook/触发失败：{exc}")

        _enters = [m.get("payload") for m in msgs
                   if m.get("type") == "send"
                   and isinstance(m.get("payload"), dict)
                   and m["payload"].get("phase") == "enter"
                   and m["payload"].get("label") == "abi-slot-test"]

    check("探针真实捕获到该函数的 enter 事件", bool(_enters),
          f"{len(_enters)} 条")
    if _enters:
        _got = _enters[0].get("args")
        check("端到端：args 读作 [0x1000, 2.5, 42]（真值）",
              _got == ["0x1000", 2.5, 42], f"{_got}")
        check("端到端：没有 args_error（三个参数全部读到了）",
              not _enters[0].get("args_error"),
              f"{_enters[0].get('args_error')}")

    # ---- 非空转反证：把**旧的双计数器逻辑**原样写一遍，看它是否真的读错
    # 这条是关键：如果旧逻辑也能读对，那上面的断言就是恒真的（没有价值）。
    _legacy = None
    if _abi_addr:
        _legacy_code = (
            "(function(){"
            " var addr = ptr(" + json.dumps(_abi_addr) + ");"
            " var got = null;"
            " var l = Interceptor.attach(addr, { onEnter: function(args){"
            "   var specs = ['ptr','float','int'];"
            "   var intIdx = 0, fpIdx = 0, vals = [];"
            "   for (var i = 0; i < specs.length; i++) {"
            "     var s = specs[i];"
            "     if (s === 'float' || s === 'double') {"
            "       var reg = this.context['xmm' + fpIdx];"
            "       var v = null;"
            "       try { if (reg instanceof ArrayBuffer) {"
            "         v = new DataView(reg).getFloat32(0, true);"
            "       } else if (reg && reg.f) { v = reg.f[0]; } } catch (e) {}"
            "       vals.push(v); fpIdx++;"
            "     } else {"
            "       var r = null; try { r = args[intIdx]; } catch (e) {}"
            "       vals.push(r === undefined || r === null ? null : r.toString());"
            "       intIdx++;"
            "     }"
            "   }"
            "   got = vals;"
            " }});"
            " var f = new NativeFunction(addr, 'int', ['pointer','float','int']);"
            " f(ptr('0x1000'), 2.5, 42);"
            " l.detach();"
            " return got;"
            "})()"
        )
        try:
            _lr = script.exports_sync.eval_js(_legacy_code)
            _legacy = _lr.get("value") if isinstance(_lr, dict) else None
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️  反证脚本失败：{exc}")

    check("反证：旧的「双计数器」逻辑对同一输入**确实读错**",
          isinstance(_legacy, list) and _legacy != ["0x1000", 2.5, 42],
          f"旧逻辑读到 {_legacy}（真值是 ['0x1000', 2.5, 42]）")

    # ================================================================
    # Batch 6：受控返回值覆写 `override_retval`（端到端闭环）
    # ================================================================
    #
    # 必须端到端验，不能只做源码扫描 —— 因为「写回去了没有」只有调用方拿到的
    # 实际返回值能回答。这里造两个真函数（int 与 float），走探针真实的
    # `hook` RPC + `onLeave` 路径，断言：
    #   ① 调用方拿到的值真的变了；
    #   ② leave 事件里同时有 original_retval 与改写后的值（可核查）；
    #   ③ unhook 之后**恢复原值**（这是「受控」二字的含义）。
    print()
    print("-" * 74)
    print("Batch 6：override_retval 端到端（改得掉、可核查、卸得掉）")
    print("-" * 74)

    _C6 = ("int gm_i(int x) { return x + 1; } "
           "float gm_f(float x) { return x * 2.0f; }")
    _mk = (
        "(function(){ var cm = new CModule(" + json.dumps(_C6) + ");"
        " globalThis.__gm_c6 = cm;"
        " return { i: cm.gm_i.toString(), f: cm.gm_f.toString() }; })()"
    )
    _addr = None
    try:
        _r6 = script.exports_sync.eval_js(_mk)
        _addr = _r6.get("value") if isinstance(_r6, dict) else None
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ⚠️  CModule 创建失败：{exc}")
    check("造出 int 与 float 两个原生靶子", isinstance(_addr, dict)
          and bool(_addr.get("i")) and bool(_addr.get("f")), f"{_addr}")

    def _call_i(x):
        r = script.exports_sync.eval_js(
            "(function(){ var f = new NativeFunction(ptr("
            + json.dumps(_addr["i"]) + "), 'int', ['int']);"
            " return f(" + str(x) + "); })()")
        return r.get("value") if isinstance(r, dict) else None

    def _call_f(x):
        r = script.exports_sync.eval_js(
            "(function(){ var f = new NativeFunction(ptr("
            + json.dumps(_addr["f"]) + "), 'float', ['float']);"
            " return f(" + str(x) + "); })()")
        return r.get("value") if isinstance(r, dict) else None

    if isinstance(_addr, dict) and _addr.get("i"):
        _base_int = _call_i(10)
        _base_float = _call_f(3.0)
        check("基准：gm_i(10)==11 且 gm_f(3.0)==6.0（靶子可用）",
              _base_int == 11 and _base_float == 6.0,
              f"int={_base_int} float={_base_float}")

        # ---- ① 整型覆写
        _hk_i = None
        try:
            _hk_i = script.exports_sync.hook({
                "address": _addr["i"], "args": ["int"], "retval_type": "int",
                "override_retval": "9999", "label": "c6-int",
                "max_calls": 10,
            })
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️  整型 Hook 失败：{exc}")
        check("整型覆写：hook 返回 ok 且回传 override_retval",
              isinstance(_hk_i, dict) and _hk_i.get("ok") is True
              and str(_hk_i.get("override_retval")) == "9999",
              f"{_hk_i if not isinstance(_hk_i, dict) else {k: _hk_i.get(k) for k in ('ok', 'override_retval')}}")
        _after_int = _call_i(10)
        check("★ 整型覆写生效：调用方拿到 9999（不再是 11）",
              _after_int == 9999, f"实际 {_after_int}")

        # ---- ② 浮点覆写（指导书说「不可覆写」，实测可）
        _hk_f = None
        try:
            _hk_f = script.exports_sync.hook({
                "address": _addr["f"], "args": ["float"],
                "retval_type": "float", "override_retval": "99.5",
                "label": "c6-float", "max_calls": 10,
            })
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️  浮点 Hook 失败：{exc}")
        check("浮点覆写：hook 返回 ok",
              isinstance(_hk_f, dict) and _hk_f.get("ok") is True,
              f"{_hk_f if not isinstance(_hk_f, dict) else _hk_f.get('ok')}")
        _after_float = _call_f(3.0)
        check("★ 浮点覆写生效：调用方拿到 99.5（不再是 6.0）",
              _after_float is not None and abs(_after_float - 99.5) < 0.01,
              f"实际 {_after_float}"
              "（指导书曾断言浮点不可覆写，实测推翻）")

        # ---- ③ 事件里带 original_retval（可核查改前/改后）
        import time as _time6
        _time6.sleep(0.4)
        _leaves = [m.get("payload") for m in msgs
                   if m.get("type") == "send"
                   and isinstance(m.get("payload"), dict)
                   and m["payload"].get("phase") == "leave"
                   and m["payload"].get("label") in ("c6-int", "c6-float")]
        check("leave 事件带 original_retval（改前值可核查）",
              bool(_leaves) and all("original_retval" in e for e in _leaves),
              f"{len(_leaves)} 条 leave")
        _int_leave = next((e for e in _leaves if e.get("label") == "c6-int"), None)
        if _int_leave:
            check("整型 leave：original_retval=11 且 retval_overridden=True",
                  _int_leave.get("original_retval") == 11
                  and _int_leave.get("retval_overridden") is True,
                  f"original={_int_leave.get('original_retval')} "
                  f"overridden={_int_leave.get('retval_overridden')}")
        _f_leave = next((e for e in _leaves if e.get("label") == "c6-float"), None)
        if _f_leave:
            check("浮点 leave：original_retval=6.0 且 retval_overridden=True",
                  _f_leave.get("original_retval") == 6.0
                  and _f_leave.get("retval_overridden") is True,
                  f"original={_f_leave.get('original_retval')} "
                  f"overridden={_f_leave.get('retval_overridden')}")

        # ---- ④ hookStatus 里能看见覆写配置
        _st = None
        try:
            _st = script.exports_sync.hook_status()
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️  hook_status 失败：{exc}")
        _rows = (_st or {}).get("hooks") or []
        _row_i = next((r for r in _rows if r.get("label") == "c6-int"), None)
        check("hookStatus 报出该 Hook 的 override_retval（状态可查）",
              _row_i is not None and str(_row_i.get("override_retval")) == "9999",
              f"{_row_i}")

        # ---- ⑤ unhook 后必须恢复原值（「受控」的含义）
        for _hid in (_hk_i or {}).get("hook_id"), (_hk_f or {}).get("hook_id"):
            if _hid:
                try:
                    script.exports_sync.unhook(_hid)
                except Exception as exc:                      # noqa: BLE001
                    print(f"  ⚠️  unhook({_hid}) 失败：{exc}")
        _restored_int = _call_i(10)
        _restored_float = _call_f(3.0)
        check("★ unhook 后整型恢复原值 11（覆写是受控的、可撤销）",
              _restored_int == 11, f"实际 {_restored_int}")
        check("★ unhook 后浮点恢复原值 6.0",
              _restored_float is not None and abs(_restored_float - 6.0) < 0.01,
              f"实际 {_restored_float}")

        # ---- ⑥ 非法覆写值必须当场拒绝（不假成功）
        _bad = None
        try:
            _bad = script.exports_sync.hook({
                "address": _addr["i"], "args": ["int"], "retval_type": "int",
                "override_retval": "not-a-number", "label": "c6-bad",
            })
        except Exception as exc:                              # noqa: BLE001
            _bad = {"ok": False, "error": str(exc)}
        check("非法覆写值被当场拒绝（ok=False + 说明原因）",
              isinstance(_bad, dict) and _bad.get("ok") is False
              and bool(_bad.get("error")),
              f"{_bad if not isinstance(_bad, dict) else str(_bad.get('error'))[:70]}")
        # 反证：拒绝之后靶子**必须仍然可调用**（不能因为装 Hook 失败把它弄坏）
        check("被拒绝后靶子仍返回原值 11（没有留下半装状态的 Hook）",
              _call_i(10) == 11, f"实际 {_call_i(10)}")

    script.unload()
    print("  ✅ 脚本已卸载")
finally:
    try:
        session.detach()
    except Exception:
        pass

print()
print("=" * 74)
print(f"通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    for f_ in FAIL:
        print(f"  ❌ {f_}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
