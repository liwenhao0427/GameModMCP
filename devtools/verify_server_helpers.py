"""验收：`server.py` / `session.py` 里的**纯函数助手**（P2-5 剩余项）。

这几处原来都有「看起来对、实际是错的」行为，而且都不需要游戏就能验证：

  1. `_split_argv()` —— `launch_game(args=...)` 原来用 `args.split()`，
     含空格的路径会被拆成多个参数。改的时候差点照抄「应该用 `shlex.split`」
     这条建议，实测发现**更糟**：

         shlex.split(r'--path C:\\Games\\My Game\\a.exe --x')
         posix=True （默认）-> ['--path', 'C:GamesMy', 'Gamea.exe', '--x']
         posix=False        -> ['--path', 'C:\\\\Games\\\\My', 'Game\\\\a.exe', '--x']

     默认 posix 模式把反斜杠当转义符，而 Windows 路径全是反斜杠 ——
     照那条建议改会把 `C:\\Games\\Game\\a.exe` 变成 `C:GamesGamea.exe`。
     所以自己按 Windows 习惯拆，本脚本把这几条都钉住。

  2. `_field_read_width()` —— `read_field_value` 原来**固定读 8 字节**，
     4 字节字段会连带读到相邻字段的字节：既给出无意义的 `as_int64`
     （两个字段拼出来的数），也会在字段贴近页尾时把一次本可成功的读取
     变成假失败。

  3. `_parse_arg_specs()` / `_parse_retval_type()` —— Hook 的规格名原来
     照单全收，写错的 `flaot` 会静默按指针读一个整型寄存器，给出「看着
     有值、其实是错值」的结果。契约第 3 条要求收下的参数真的影响行为，
     所以写错必须报错。

  4. `_retval_spec_for()` —— 返回值读法不能一律按指针：float / double 在
     XMM0 而不在 RAX，按 ptr 读会给出毫无意义的整数（实测 `Mathf.Abs(-2.5f)`
     得到 `0x20da0a48360`）。

  5. `_overlapping_hook()` —— `write_memory` 的护栏：写已被 Hook 的函数入口
     （哪怕写回刚读到的同样字节）实测会让游戏数秒内崩在 `0xc0000005`。

这些都是纯函数，所以本脚本**不需要任何样本**，永远会跑。
真正端到端的那部分（拿真实字段偏移写标记值再读回来、捕获参数与返回值）
在 `verify_mcp_extra.py` / `verify_mcp_tools.py` 里，那边要一个活着的游戏。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import gamemod_mcp.server as S                            # noqa: E402

PASS = 0
FAIL = 0
WARN = 0


def check(label: str, cond: bool, detail: str = "", warn_only: bool = False) -> bool:
    global PASS, FAIL, WARN
    if cond:
        print(f"  \u2705 {label}" + (f" \u2014 {detail}" if detail else ""))
        PASS += 1
        return True
    if warn_only:
        print(f"  \u26a0\ufe0f  {label}" + (f" \u2014 {detail}" if detail else ""))
        WARN += 1
        return False
    print(f"  \u274c {label}" + (f" \u2014 {detail}" if detail else ""))
    FAIL += 1
    return False


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


#: (输入, 期望)。反斜杠在源码里写成 `\\`，即运行期就是一个反斜杠。
ARGV_CASES: list[tuple[str, list[str]]] = [
    ("", []),
    ("   ", []),
    ("--x", ["--x"]),
    ("-a b", ["-a", "b"]),
    ("  -a   -b  ", ["-a", "-b"]),                       # 连续空白只算一个分隔
    ("-a\t-b", ["-a", "-b"]),                            # tab 也是空白
    ("-n 3", ["-n", "3"]),
    # 不带引号的含空格路径 —— Windows 上也必须是两个 token（这是正确行为，
    # 不是 bug：命令行里含空格的路径必须加引号）
    (r"C:\Games\My Game\a.exe", [r"C:\Games\My", r"Game\a.exe"]),
    # 带引号：一个 token，且**反斜杠一个都不能少**
    (r'-path "C:\Games\My Game\a.exe" -v',
     ["-path", r"C:\Games\My Game\a.exe", "-v"]),
    # 引号只包住一段
    (r'--name="a b"', ["--name=a b"]),
    # 不带引号、不含空格的反斜杠路径：一个 token，反斜杠原样保留
    # （最容易被 shlex 吃掉的情形）
    (r"C:\Games\Garden\GameAssembly.dll",
     [r"C:\Games\Garden\GameAssembly.dll"]),
    (r'"C:\Games\My Game\GameAssembly.dll"',
     [r"C:\Games\My Game\GameAssembly.dll"]),
    # 未闭合引号：一直引到串尾（不报错）
    ('"unterminated', ["unterminated"]),
    ('--a "b', ["--a", "b"]),
    # 空引号不产生空 token（已在 docstring 里写明）
    ('--empty ""', ["--empty"]),
]

#: (类型名, 期望宽度)
WIDTH_CASES: list[tuple[str, int]] = [
    # 1 字节
    ("System.Boolean", 1), ("bool", 1), ("System.Byte", 1),
    ("byte", 1), ("System.SByte", 1), ("sbyte", 1),
    # 2 字节
    ("System.Char", 2), ("char", 2), ("System.Int16", 2), ("short", 2),
    ("System.UInt16", 2), ("ushort", 2),
    # 4 字节
    ("System.Int32", 4), ("int", 4), ("System.UInt32", 4), ("uint", 4),
    ("System.Single", 4), ("float", 4),
    # 8 字节
    ("System.Int64", 8), ("long", 8), ("System.UInt64", 8), ("ulong", 8),
    ("System.Double", 8), ("double", 8),
    ("System.IntPtr", 8), ("System.UIntPtr", 8),
    # 引用类型 = 指针 = 8 字节（这是原来固定 8 的行为，不能改坏）
    ("System.String", 8), ("UnityEngine.TextAsset", 8),
    ("System.Threading.CancellationTokenSource", 8),
    # 查不到 / 空 / None → 8（保守，等同原行为）
    ("", 8), ("MyNamespace.MyUnknownStruct", 8),
    ("  System.Int32  ", 4),                             # 前后空白要容忍
]


def main() -> int:
    section("1. `_split_argv()` —— launch_game 的命令行拆分")

    bad: list[str] = []
    for text, want in ARGV_CASES:
        got = S._split_argv(text)
        if got != want:
            bad.append(f"{text!r} -> {got!r}（期望 {want!r}）")
    check(f"{len(ARGV_CASES)} 条拆分用例全部正确", not bad,
          f"不符 {len(bad)} 条：{bad[:3]}" if bad else "含引号/反斜杠/tab/未闭合")

    # 把「这条修的是什么」变成可判定的断言，而不只是几条用例：
    # 原来 args.split() 的行为必须**为假**，否则这条修复等于没做。
    spaced = r'"C:\Games\My Game\a.exe"'
    old_behaviour = spaced.replace('"', "").split()
    new_behaviour = S._split_argv(spaced)
    check("含空格路径不再被拆成多个参数",
          len(new_behaviour) == 1 and len(old_behaviour) > 1,
          f"旧 {old_behaviour} -> 新 {new_behaviour}")

    # 反斜杠必须原样保留 —— 这正是 `shlex.split` 默认模式会破坏的地方
    preserve = r'"C:\Games\My Game\a.exe"'
    got = S._split_argv(preserve)
    check("反斜杠不当作转义符（`shlex.split` 默认模式会吃掉它）",
          got == [r"C:\Games\My Game\a.exe"],
          f"得到 {got!r}")

    # 顺带把「为什么不直接用 shlex」也钉住：一旦有人换成 shlex.split，
    # 本条会立刻报红，比留一句注释可靠。
    import shlex
    shlex_posix = shlex.split(r'--path C:\Games\My Game\a.exe')
    ours = S._split_argv(r'--path "C:\Games\My Game\a.exe"')
    check("`shlex.split` 默认模式确实会破坏 Windows 路径（换掉它的理由成立）",
          shlex_posix != ours and r"C:\Games" not in " ".join(shlex_posix),
          f"shlex={shlex_posix} 我们={ours}")

    section("2. `_field_read_width()` —— 按字段类型决定读几个字节")

    badw: list[str] = []
    for type_name, want in WIDTH_CASES:
        got_w = S._field_read_width(type_name)
        if got_w != want:
            badw.append(f"{type_name!r} -> {got_w}（期望 {want}）")
    check(f"{len(WIDTH_CASES)} 条宽度用例全部正确", not badw,
          f"不符 {len(badw)} 条：{badw[:3]}" if badw else
          "bool/int/float/long/引用类型/未知")
    check("None（拿不到类型名）按 8 字节",
          S._field_read_width(None) == 8, "保守，等同原行为")

    # 非空转：这个函数不能是个常量。原来「固定读 8」的写法会让下面这条为假。
    widths = {S._field_read_width(t) for t in
              ("System.Boolean", "System.Int16", "System.Int32",
               "System.Int64", "UnityEngine.TextAsset")}
    check("宽度真的随类型变化（不是一个恒为 8 的常量）",
          widths == {1, 2, 4, 8}, f"取到的宽度集合 = {sorted(widths)}")
    check("4 字节字段不会读成 8 字节（这就是原来的 bug）",
          S._field_read_width("System.Int32") == 4,
          "原来固定读 8，会连带读到相邻字段的 4 个字节")

    section("3. Hook 参数规格校验 —— 收下的参数必须真的影响行为")
    # 契约第 3 条：`args_type` 以前照单全收，写错的规格名（例如 `flaot`）
    # 会落到探针的 `default: raw.toString()` —— 于是**悄悄**按指针读一个
    # 整型寄存器，调用方以为拿到的是 float，实际是个地址字符串。
    # 这类「看起来有值、其实是错值」比直接报错危险得多。
    for good in ("int", "uint", "long", "ulong", "bool", "float", "double",
                 "ptr", "string", "cstring"):
        got_g = S._parse_arg_specs(good)
        if got_g != [good]:
            check(f"合法规格 {good} 被接受", False, f"得到 {got_g}")
            break
    else:
        check("10 个合法规格名全部被接受", True, "int/uint/long/ulong/bool/"
              "float/double/ptr/string/cstring")
    check("逗号分隔 + 前后空白被容忍",
          S._parse_arg_specs(" ptr , int ") == ["ptr", "int"],
          str(S._parse_arg_specs(" ptr , int ")))
    check("空串 → 空列表（不解析参数）", S._parse_arg_specs("") == [])
    for bad_s in ("flaot", "intt", "Int", "float32", "ptr,int,flaot"):
        try:
            got_b = S._parse_arg_specs(bad_s)
            check(f"非法规格 {bad_s!r} 必须报错", False, f"却返回 {got_b}")
        except ValueError as exc:
            check(f"非法规格 {bad_s!r} 报错并列出可用值",
                  bad_s.split(",")[-1] in str(exc), str(exc)[:60])
    check("retval_type 留空 = 按静态返回类型自动推断",
          S._parse_retval_type("") == "")
    try:
        S._parse_retval_type("flaot")
        check("非法 retval_type 必须报错", False)
    except ValueError as exc:
        check("非法 retval_type 报错", "flaot" in str(exc), str(exc)[:50])

    section("4. `_retval_spec_for()` —— 返回值读法不能一律按指针")
    # float / double 的返回值在 XMM0，不在 RAX：按 ptr 读会给出一个
    # 看着像地址、其实毫无意义的整数（实测 Mathf.Abs(-2.5f) 得到
    # 0x20da0a48360）。所以这个映射必须自动做对。
    from gamemod_mcp.runtime import session as _sess          # noqa: E402
    RETVAL_CASES = [
        ("System.Single", "float"), ("System.Double", "double"),
        ("System.Boolean", "bool"), ("System.Int32", "int"),
        ("System.UInt32", "uint"), ("System.Int64", "long"),
        ("System.UInt64", "ulong"), ("System.String", "string"),
        # 认不出来的一律 ptr：原样给出寄存器值，不含糊
        ("UnityEngine.Vector3", "ptr"), ("MyGame.SomeClass", "ptr"),
        ("", "ptr"), (None, "ptr"),
    ]
    badr: list[str] = []
    for tname, want in RETVAL_CASES:
        got_r = _sess._retval_spec_for(tname)
        if got_r != want:
            badr.append(f"{tname!r} -> {got_r}（期望 {want}）")
    check(f"{len(RETVAL_CASES)} 条返回类型映射全部正确", not badr,
          f"不符 {len(badr)} 条：{badr[:3]}" if badr else
          "浮点→float/double，整型按宽度，其余→ptr")
    check("浮点返回类型不会退化成 ptr（这就是那个 bug）",
          _sess._retval_spec_for("System.Single") != "ptr",
          "按 ptr 读 RAX 会给出无意义的整数")
    check("映射真的随类型变化（不是恒为 ptr 的常量）",
          len({_sess._retval_spec_for(t) for t, _ in RETVAL_CASES}) >= 5,
          str(sorted({_sess._retval_spec_for(t) for t, _ in RETVAL_CASES})))

    # ---- 短名 / CIL 名也要认（Mono 运行时走的就是这条路）----
    # `mono_type_get_name` 给的是**短名**（agent.js 的 monoTypeName 注释写明
    # 「Mono 给的是短名（int / string）」，而静态元数据给的是 `System.Single`）。
    # 只认长名的话，Mono 上 float 返回值会落到默认的 ptr —— 那正是这张表
    # 本来要防的缺陷（按 ptr 读 XFloat 会给出无意义的整数），在 Mono 上照旧。
    SHORT_RETVAL_CASES = [
        ("float", "float"), ("single", "float"), ("Single", "float"),
        ("System.Single", "float"),
        ("double", "double"), ("Double", "double"),
        ("bool", "bool"), ("boolean", "bool"), ("Boolean", "bool"),
        ("int", "int"), ("int32", "int"), ("Int32", "int"),
        ("uint", "uint"), ("uint32", "uint"),
        ("int16", "int"), ("short", "int"), ("uint16", "uint"),
        ("ushort", "uint"), ("byte", "uint"), ("sbyte", "int"),
        ("char", "uint"),
        ("long", "long"), ("int64", "long"),
        ("ulong", "ulong"), ("uint64", "ulong"),
        ("string", "string"), ("String", "string"),
        # `System.` 前缀的短名也要认（两种 Mono 版本都可能给这种写法）
        ("System.Int32", "int"), ("System.String", "string"),
        # 认不出来的照旧给 ptr
        ("Vector3", "ptr"), ("MyGame.SomeClass", "ptr"),
        ("MyCompany.Int", "ptr"),
    ]
    bads: list[str] = []
    for tname, want in SHORT_RETVAL_CASES:
        got_s = _sess._retval_spec_for(tname)
        if got_s != want:
            bads.append(f"{tname!r} -> {got_s}（期望 {want}）")
    check(f"{len(SHORT_RETVAL_CASES)} 条短名/长名返回类型映射全部正确", not bads,
          f"不符 {len(bads)} 条：{bads[:3]}" if bads else
          "C# 短名 / CIL 名 / System.* 长名三种写法都认")
    check("短名 float 不再退化成 ptr（这就是 Mono 上的那个缺陷）",
          _sess._retval_spec_for("float") == "float"
          and _sess._retval_spec_for("single") == "float",
          "按 ptr 读 RAX 会给出无意义的整数")
    # 短名兜底**不能**把用户自定义类型也吞进去：`MyCompany.Int` 若按短名
    # 命中 int，就会把一个引用当 4 字节整数读 —— 比给 ptr 危险得多。
    check("带非 System 命名空间的类型不按短名猜（一律 ptr）",
          _sess._retval_spec_for("MyCompany.Int") == "ptr"
          and _sess._retval_spec_for("UnityEngine.Vector3") == "ptr",
          "短名兜底只对「无命名空间」或「System.*」生效")

    section("5. `_overlapping_hook()` —— write_memory 的护栏")
    # 实测：写已被 Hook 的函数入口（哪怕写回刚读到的同样字节）会让游戏
    # 在数秒内崩在 0xc0000005；同一操作写在模块头（base）上则完全无害。
    # 这个纯函数决定「要不要拦」，所以在这里钉住它。
    class _StubRT:
        def __init__(self, hooks):
            self._hooks = hooks

        def hook_status(self):
            return {"ok": True, "hooks": self._hooks}

    class _StubState:
        def __init__(self, hooks, boom=False):
            self.runtime = _StubRT(hooks)
            self._boom = boom

    hooks = [{"hook_id": "h1", "label": "verify.frameCount",
              "target": "0x7ffa2cc4b170"}]
    st_h = _StubState(hooks)
    check("写入被 Hook 的入口 → 命中（必须拦）",
          (S._overlapping_hook(st_h, "0x7ffa2cc4b170", 16) or {}).get("hook_id") == "h1",
          "首地址相同")
    check("写入入口 +4 字节处 → 仍命中（跳转指令有多字节）",
          S._overlapping_hook(st_h, "0x7ffa2cc4b174", 4) is not None,
          f"余量 {S._HOOK_ENTRY_SLACK} 字节")
    check("写入入口 +32 字节处 → 不命中（不是整段都拦）",
          S._overlapping_hook(st_h, "0x7ffa2cc4b190", 4) is None)
    check("写入模块头（base）→ 不命中（实测这样写无害）",
          S._overlapping_hook(st_h, "0x7ffa29f70000", 16) is None)
    check("写入跨越入口的一段（起于入口前 8 字节、长 32）→ 命中",
          S._overlapping_hook(st_h, "0x7ffa2cc4b168", 32) is not None,
          "重叠判定要按区间，不是只看起点")
    check("没有 Hook 时一律放行",
          S._overlapping_hook(_StubState([]), "0x7ffa2cc4b170", 16) is None)
    check("地址解析不了时放行（护栏只拦已知会崩的写法）",
          S._overlapping_hook(st_h, "不是地址", 16) is None)
    check("size<=0 时放行", S._overlapping_hook(st_h, "0x7ffa2cc4b170", 0) is None)

    class _BoomRT:
        def hook_status(self):
            raise RuntimeError("探针不通（模拟）")

    class _BoomState:
        runtime = _BoomRT()

    check("hook_status 抛异常时放行（不把写入变成不可用）",
          S._overlapping_hook(_BoomState(), "0x7ffa2cc4b170", 16) is None)

    section("6. `launch_game(args=...)` 必须把解析出的 argv 透传下去")
    # 这条原来是「说了话不干活」：`launch_game` 用 `_split_argv` 解析出了 argv、
    # 还在返回里回传了它，却**没往下传** —— `ensure_runtime` 的签名里根本没有
    # args，`spawn_and_attach` 却在第 2 个位置早就支持它。于是调用方看到
    # `argv: [...]`（以为生效了），游戏实际上是**按没有参数**启动的。
    # 这类缺陷不会被任何异常暴露，只能靠拦截底层调用钉住。
    import tempfile                                          # noqa: E402
    import gamemod_mcp.engine.state as _st_mod               # noqa: E402

    captured: dict = {}

    class _FakeRuntime:
        pid = 424242

    def _fake_ensure_runtime(**kw):
        captured.clear()
        captured.update(kw)
        return {"ok": True, "runtime": {"pid": 424242}}

    # 用一个真实存在的文件当 exe：`launch_game` 会先做 is_file 检查
    with tempfile.TemporaryDirectory() as td:
        fake_exe = os.path.join(td, "Game.exe")
        with open(fake_exe, "wb") as fh:
            fh.write(b"MZ")                                  # 内容无关，只要存在

        orig = (S.ensure_runtime, S._has_live_session,
                S._same_named_processes, _st_mod.ensure_runtime)
        try:
            S.ensure_runtime = _fake_ensure_runtime
            S._has_live_session = lambda: False
            S._same_named_processes = lambda exe: []

            args_text = r'-popupwindow -path "C:\My Game\a.txt"'
            res = S.launch_game(exe=fake_exe, args=args_text)
        finally:
            (S.ensure_runtime, S._has_live_session,
             S._same_named_processes, _st_mod.ensure_runtime) = orig

        want_argv = ["-popupwindow", "-path", r"C:\My Game\a.txt"]
        check("launch_game 调通了（返回 ok）", res.get("ok") is True,
              str(res.get("error", ""))[:80])
        check("`launch_game` 返回里如实给出 argv",
              res.get("argv") == want_argv, f"得到 {res.get('argv')!r}")
        check("argv 被传给了 `ensure_runtime(args=...)`",
              captured.get("args") == want_argv,
              f"底层收到 args={captured.get('args')!r}")
        check("spawn 模式与目标 exe 也照常传下去",
              captured.get("spawn") is True
              and captured.get("launcher_exe") == fake_exe,
              f"spawn={captured.get('spawn')!r} exe={captured.get('launcher_exe')!r}")

        # 非空转反证：把透传去掉（复刻修复前的调用），断言上面那条会失败。
        # 只留「返回里有 argv」这一半，正是原来的样子 —— 看着对、其实没生效。
        captured.clear()
        try:
            S.ensure_runtime = _fake_ensure_runtime
            S._has_live_session = lambda: False
            S._same_named_processes = lambda exe: []
            S.ensure_runtime(spawn=True, launcher_exe=fake_exe)   # 旧写法：无 args
        finally:
            (S.ensure_runtime, S._has_live_session,
             S._same_named_processes, _st_mod.ensure_runtime) = orig
        check("反证：不透传时底层拿不到 args（说明上面那条不是空转）",
              captured.get("args") is None,
              f"旧写法底层 args={captured.get('args')!r}")

    # `ensure_runtime` 的签名必须真的收这个参数（否则只是 server 侧自说自话）
    import inspect                                           # noqa: E402
    sig = inspect.signature(_st_mod.ensure_runtime)
    check("`ensure_runtime` 签名里有 args 参数",
          "args" in sig.parameters, str(sig))
    check("`ensure_runtime` 把 args 转发给 `spawn_and_attach`",
          "args=args" in inspect.getsource(_st_mod.ensure_runtime),
          "纯签名检查挡不住「收了但不用」")

    # ================================================================
    # 8. 分层：字段/验收求值的实现已下沉到 engine/，且**注入点仍生效**
    #
    # 这次重构把 ~440 行纯逻辑从 server.py 挪到 engine/fields.py 与
    # engine/verifier.py。风险不在「挪没挪对」，而在两处**兼容契约**：
    #   ① 既有测试直接访问 `S._field_read_width` 等属性 → 必须 re-export；
    #   ② `verify_verify_state.py` 会 monkeypatch `S._screen_mod` 与
    #      `S.RAW_TOOLS[...]` → server 必须**每次调用时**把它们注入 engine，
    #      而不是让 engine 自己持有一份引用（那样 monkeypatch 会被绕过，
    #      测试就变成对一个副本求值）。
    # ②是这次重构最容易静默做错的地方，所以单独钉住。
    print()
    print("=" * 74)
    print("8. 分层：helper 下沉到 engine/，注入点仍生效")
    print("=" * 74)

    import types                                             # noqa: E402

    from gamemod_mcp.engine import fields as _fields_mod      # noqa: E402
    from gamemod_mcp.engine import verifier as _verifier_mod  # noqa: E402

    for nm in ("_FIELD_WIDTH", "_FIELD_WRITE_KIND", "_field_read_width",
               "_field_write_kind", "_encode_field_value",
               "_resolve_enum_underlying", "_explain_bytes",
               "_field_value_as_text", "_values_equal", "_eval_condition"):
        _has = hasattr(S, nm)
        check(f"server 仍可按原名访问 {nm}（re-export 兼容）", _has,
              "" if _has else f"getattr(S, {nm!r}) 取不到 —— "
                              "既有测试会 AttributeError")
    check("实现已真的下沉（不再是 server 里定义的）",
          S._field_read_width is _fields_mod._field_read_width
          and S._values_equal is _verifier_mod._values_equal,
          "同名绑定指向 engine 模块里的函数")
    # 反证：如果只是「在 server 里又抄了一份」，id 不会相同。
    check("反证：两份实现不是各写一份（对象身份相同，不是复制）",
          S._encode_field_value is _fields_mod._encode_field_value)

    # ---- 注入点 ①：_screen_mod 的 monkeypatch 必须影响求值
    _real_screen2 = S._screen_mod()
    _fake2 = types.ModuleType("fake_screen_h")
    _fake2.__dict__.update(_real_screen2.__dict__)
    _fake2.find_window_rect = lambda t: {"ok": True, "title": str(t)}
    _orig_sm = S._screen_mod
    S._screen_mod = lambda: _fake2
    try:
        _r_patched = S._eval_condition("window", target="分层测试窗口")
    finally:
        S._screen_mod = _orig_sm
    _r_restored = S._eval_condition("window", target="分层测试窗口")
    check("monkeypatch `S._screen_mod` **真的**影响 engine 层求值",
          _r_patched.get("hit") is True and _r_restored.get("hit") is not True,
          f"patch 后 hit={_r_patched.get('hit')} / 还原后 "
          f"hit={_r_restored.get('hit')}")

    # ---- 注入点 ②：RAW_TOOLS 的替换必须影响求值
    _orig_fi2 = S.RAW_TOOLS.get("find_instances")
    S.RAW_TOOLS["find_instances"] = lambda **kw: {"ok": True, "count": 7,
                                                  "instances": []}
    try:
        _r_tools = S._eval_condition("instances", name="Whatever", min_count=5)
    finally:
        if _orig_fi2 is not None:
            S.RAW_TOOLS["find_instances"] = _orig_fi2
    check("monkeypatch `S.RAW_TOOLS['find_instances']` **真的**影响求值",
          _r_tools.get("hit") is True
          and _r_tools.get("detail", {}).get("count") == 7,
          f"detail={_r_tools.get('detail')}")

    # ---- server.py 只剩编排，不再定义这些纯函数
    import ast as _ast                                       # noqa: E402
    _tree = _ast.parse((samples.SRC / "gamemod_mcp" / "server.py"
                        ).read_text(encoding="utf-8"))
    _defined = {n.name for n in _tree.body
                if isinstance(n, _ast.FunctionDef)}
    _moved = {"_field_read_width", "_field_write_kind", "_encode_field_value",
              "_resolve_enum_underlying", "_explain_bytes",
              "_eval_condition_inner", "_field_value_as_text", "_values_equal"}
    _still = sorted(_moved & _defined)
    check("那些纯函数已不在 server.py 里定义（真的下沉了）",
          not _still, f"仍在 server.py 定义：{_still}")
    check("`_eval_condition` **保留**在 server.py（它是注入用的薄包装）",
          "_eval_condition" in _defined,
          "去掉它会让 monkeypatch 失效（见上面两条注入断言）")

    # ================================================================
    # 9. `override_retval`：受控返回值覆写的参数相容性（不需要游戏）
    print()
    print("=" * 74)
    print("9. override_retval：参数相容性校验（不假成功）")
    print("=" * 74)

    # 空串是**默认值**，表示「只观察不改」——必须 100% 兼容旧调用。
    _v, _e = S._parse_override_retval("", "int")
    check("override_retval 留空 = 不覆写（保持原行为）",
          _v == "" and _e is None, f"value={_v!r} err={_e!r}")

    # 可覆写的类型逐一放行
    for _t in ("int", "uint", "long", "ulong", "bool", "ptr", "float", "double"):
        _v2, _e2 = S._parse_override_retval("123", _t)
        if _e2 is not None or _v2 != "123":
            check(f"retval_type={_t} 允许覆写", False, f"err={_e2}")
            break
    else:
        check("8 个可覆写类型（含 float/double）全部放行", True,
              "int/uint/long/ulong/bool/ptr/float/double")

    # 不支持的类型必须**明确拒绝**并说清替代方案
    for _bad in ("string", "cstring"):
        _v3, _e3 = S._parse_override_retval("x", _bad)
        check(f"retval_type={_bad} 拒绝覆写（要换托管对象引用）",
              _v3 == "" and _e3 is not None and "eval_script" in str(_e3),
              str(_e3)[:70])

    # 反证：float **必须**在允许表里。指导书曾断言「浮点返回值不可覆写、
    # 应当明确拒绝」—— 实测推翻（见 agent.js encodeRetvalOverride 的说明：
    # 整体换 XMM0 的 ArrayBuffer 有效，实测 3/3 次）。若哪天有人照那份
    # 指导书把 float 从表里删掉，这条会报红。
    check("反证：float/double **在**可覆写表里（实测可覆写，不要照旧建议拒绝）",
          "float" in S._OVERRIDABLE_RETVAL
          and "double" in S._OVERRIDABLE_RETVAL,
          "整体赋值新 ArrayBuffer 给 context.xmm0 实测有效")
    check("string/cstring **不在**可覆写表里（改托管引用属改逻辑）",
          "string" not in S._OVERRIDABLE_RETVAL
          and "cstring" not in S._OVERRIDABLE_RETVAL)

    # ---- 探针层：编码函数的源码必须体现那三条实测结论
    _agent_src = (samples.SRC / "gamemod_mcp" / "runtime" / "agent.js"
                  ).read_text(encoding="utf-8")
    check("agent.js 定义了 encodeRetvalOverride（覆写值编码抽成函数）",
          "function encodeRetvalOverride(" in _agent_src)
    check("agent.js 定义了 applyRetvalOverride（写回逻辑抽成函数）",
          "function applyRetvalOverride(" in _agent_src)
    # 浮点那条路**必须**是「整体赋值新 ArrayBuffer」，不是原地改
    _apply_start = _agent_src.find("function applyRetvalOverride(")
    _apply_body = _agent_src[_apply_start:_apply_start + 900]
    check("浮点覆写走「整体赋值新 ArrayBuffer」（原地改无效，实测）",
          "ctx['xmm0'] = ab;" in _apply_body,
          "原地 setFloat32 实测无效：那个 ArrayBuffer 是快照副本")
    check("非浮点走 retval.replace（整型返回值在 RAX，实测有效）",
          "retval.replace(" in _apply_body)
    check("覆写前记录了 original_retval（供改前/改后核查）",
          "rec.original_retval" in _agent_src,
          "没有它就无法验收「到底改没改」")
    check("hookStatus 里带上 override_retval（状态可查）",
          "row.override_retval" in _agent_src
          or "override_retval:" in _agent_src.split("hookStatus:")[-1][:2000],
          "不报出来，调用方只能靠自己记")

    # ---- 会话层与工具层：参数真的被接上了（不是「收了不用」）
    from gamemod_mcp.runtime import session as _sess2           # noqa: E402
    check("session.hook 签名收 override_retval",
          "override_retval" in inspect.signature(_sess2.RuntimeSession.hook
                                                 ).parameters)
    check("session.hook_method 签名收 override_retval",
          "override_retval" in inspect.signature(
              _sess2.RuntimeSession.hook_method).parameters)
    check("session.hook 只在非空时才把它放进 spec（空串不下发）",
          'if override_retval != "":' in inspect.getsource(
              _sess2.RuntimeSession.hook),
          "空串下发会让探针多一次无意义解析")
    _srv_src = (samples.SRC / "gamemod_mcp" / "server.py").read_text(
        encoding="utf-8")
    for _tname in ("hook_method", "hook_address"):
        _fn = S.RAW_TOOLS.get(_tname)
        check(f"工具 {_tname} 签名收 override_retval",
              _fn is not None and "override_retval" in inspect.signature(
                  _fn).parameters)
    check("server 的 hook_method 把 override_retval 真的传给会话层",
          "override_retval=override_retval" in _srv_src)
    check("server 的 hook_address 把 override_retval 真的传给会话层",
          _srv_src.count("override_retval=override_retval") >= 2,
          f"出现 {_srv_src.count('override_retval=override_retval')} 次"
          "（两个工具各一次）")

    print()
    print("=" * 74)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
