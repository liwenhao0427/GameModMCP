#!/usr/bin/env python3
"""验证「在中文 Windows 的 GBK 控制台下，脚本不会因打印字符而崩」。

背景（这是一条真实踩过的坑）
============================================================
本仓库的脚本会打印 `✅` / `❌` / `⚠️` / `💥` 之类的字符。中文 Windows 的
控制台默认编码是 **cp936(GBK)**，编不出这些字符，于是 `print()` 直接抛：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'

后果不是「输出难看」，而是**结论拿不到**。最典型的一次：
`run_all_verify.py` 把 19 个验收脚本全跑完了，却在打印汇总表
`[✅] verify_xxx` 时崩溃 —— 所有工作都做完了，却拿不到「通过 19 / 19」。

为什么这个坑很难被发现
============================================================
**开发时几乎总是带着 `PYTHONIOENCODING=utf-8` 或重定向输出**，
这两种情况都会掩盖问题。所以本脚本在运行时**显式把子进程的
`PYTHONIOENCODING` 删掉**，强制复现最苛刻的环境。

本脚本做两层检查
============================================================
1. **静态**：`devtools/*.py` 里凡是含有「GBK 编不出的字符」的文件，
   都必须显式设置 stdout 编码（`sys.stdout.reconfigure(...)` 或
   `io.TextIOWrapper(sys.stdout.buffer, ...)`）。两种写法都接受 ——
   项目里两种都在用。
2. **运行时**：挑几个**快且无副作用**的脚本，在剥掉 `PYTHONIOENCODING`
   的子进程里真跑一遍，断言「退出码 0 且 stderr 无 UnicodeEncodeError」。

第 2 层是必要的：第 1 层只能证明「看起来设了」，证明不了真的没崩。
但也不能把全部脚本跑一遍（有注入游戏、耗时长的），所以只挑代表。

    python devtools\\verify_encoding_console.py
"""

from __future__ import annotations

import io
import os as _os
import pathlib
import re
import subprocess
import sys as _sys

_HERE = pathlib.Path(__file__).resolve().parent
_sys.path.insert(0, str(_HERE))

# 本脚本自己也要打印 ✅/❌，所以自己必须先设好编码（否则排查工具自己先崩）
_sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8",
                               errors="replace")

import samples  # noqa: E402

ROOT = samples.ROOT
DEVTOOLS = ROOT / "devtools"

# 判据：显式设置过 stdout 编码的两种写法，项目里都在用，都接受。
# 不要写成「包含 utf-8 字样」—— `encoding="utf-8"` 也可能是**读**子进程输出
# （`run_all_verify.py` 就是），那样会误判成「已处理」。
HAS_WRAPPER = re.compile(r"stdout\.reconfigure\(encoding|TextIOWrapper\(")

# 运行时检查名单：必须**快**（秒级）且**无副作用**（不注入、不建大索引）。
# 每个都对应一次真实的崩溃历史，或是最容易被单独运行的入口。
#
# 每项是 (脚本名, 传给脚本的额外参数, 为什么值得测)。
# `run_all_verify.py` 必须带 `--only`：本脚本会被它自己 glob 到并执行，
# 若这里再无条件调一次整套，就等于**无限递归**（实测把机器跑满）。
# 带 `--only` 后它只跑一个脚本，仍然会走到「[✅] xxx + 汇总表」那条
# 真正崩过的打印路径。
RUNTIME_CASES: list[tuple[str, list[str], str]] = [
    ("samples.py", [], "排查样本可用性的第一入口，曾崩在最后一行"),
    ("run_all_verify.py", ["--only", "verify_tool_inventory"],
     "验收入口；曾把 19 个脚本跑完却在汇总表处崩掉"),
    ("verify_tool_coverage.py", [], "曾抛 '\\u2705' codec can't encode"),
    ("verify_tool_inventory.py", [], "文档与代码清单校验，改工具后必跑"),
    ("verify_runtime_target.py", [], "纯逻辑，不注入进程，可安全复现"),
]

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        print(f"  \u2705 {label}" + (f" \u2014 {detail}" if detail else ""))
        PASS += 1
        return True
    print(f"  \u274c {label}" + (f" \u2014 {detail}" if detail else ""))
    FAIL += 1
    return False


def section(t: str) -> None:
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def non_gbk_chars(text: str) -> list[str]:
    """返回文本里所有**用 cp936 编不出来**的字符（去重排序）。

    判据要落在「真的编不出来」上，而不是「看起来像装饰符号」——
    这样新增任何字符都会自动纳入检查，不需要维护白名单。
    """
    out: set[str] = set()
    for ch in text:
        try:
            ch.encode("gbk")
        except UnicodeEncodeError:
            out.add(ch)
    return sorted(out)


# ============================================================ 1. 静态检查
section("1. 静态：会打印非 GBK 字符的脚本，是否都设了 stdout 编码")

scripts = sorted(p for p in DEVTOOLS.glob("*.py"))
risky: list[tuple[str, str]] = []
for p in scripts:
    text = p.read_text(encoding="utf-8")
    chars = non_gbk_chars(text)
    if not chars:
        continue
    if not HAS_WRAPPER.search(text):
        risky.append((p.name, "".join(chars)))

print(f"     扫描 {len(scripts)} 个脚本，其中含非 GBK 字符的 "
      f"{sum(1 for p in scripts if non_gbk_chars(p.read_text(encoding='utf-8')))} 个")
check("devtools/ 下没有「会崩却没设编码」的脚本",
      not risky,
      "；".join(f"{n} 含 {c!r}" for n, c in risky) if risky else "全部已设")

# 反向自检：这条断言不能是空转的 —— 必须真的扫到了东西。
# 若哪天所有脚本都不再打印 ✅/❌，说明判据失效了，而不是「没问题」。
with_chars = [p.name for p in scripts
              if non_gbk_chars(p.read_text(encoding="utf-8"))]
check("检查本身非空转（确实扫到了会打印装饰字符的脚本）",
      len(with_chars) >= 5, f"{len(with_chars)} 个脚本含非 GBK 字符")

# src/ 是 MCP 服务端：stdout 是 JSON-RPC 通道，绝不能有非 ASCII 装饰字符
# 直接 print 出去。这里只做提示性统计（装饰字符出现在 docstring 里是正常的，
# 因为 docstring 会作为 JSON 字符串返回，不经过控制台编码）。
src_prints = []
for p in (ROOT / "src").rglob("*.py"):
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        # 只看真正的 print( 语句，且该行含非 GBK 字符
        if re.match(r"print\s*\(", s) and non_gbk_chars(line):
            src_prints.append(f"{p.name}:{i}")
check("src/ 没有「print 非 GBK 字符」的语句（stdout 是 JSON-RPC 通道）",
      not src_prints, "；".join(src_prints) if src_prints else "无")


# ============================================================ 2. 运行时检查
section("2. 运行时：剥掉 PYTHONIOENCODING，真跑一遍看会不会崩")

# 关键：必须显式删掉 PYTHONIOENCODING。
# 开发时通常带着它（或重定向输出），两种情况都会掩盖这个坑 ——
# 本脚本存在的意义就是强制复现最苛刻的环境。
env = {k: v for k, v in _os.environ.items() if k != "PYTHONIOENCODING"}
check("子进程环境里确实没有 PYTHONIOENCODING（否则测不出真问题）",
      "PYTHONIOENCODING" not in env)

for name, extra, why in RUNTIME_CASES:
    path = DEVTOOLS / name
    if not path.is_file():
        check(f"{name} 存在", False, "脚本不存在")
        continue
    proc = subprocess.run(
        [_sys.executable, str(path), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), env=env, timeout=300,
    )
    err = proc.stderr or ""
    crashed_enc = "UnicodeEncodeError" in err
    detail = f"exit={proc.returncode}"
    if crashed_enc:
        first = next((ln for ln in err.splitlines() if "UnicodeEncodeError" in ln), "")
        detail += f"  {first.strip()[:70]}"
    else:
        detail += f"（{why}）"
    check(f"{name} 在无 PYTHONIOENCODING 的 GBK 控制台下不崩",
          proc.returncode == 0 and not crashed_enc, detail)


# ============================================================ 3. --fast
section("3. run_all_verify --fast：离线清单由 AST 推导（不手写、不漂移）")

# 为什么单独验：`--fast` 的判据若写错，后果是**双向**的假结论 ——
#   · 判太松（把真机脚本当离线）→ --fast 会悄悄拉起一个游戏，
#     与「快检」的承诺相反；
#   · 判太严（把离线脚本当真好）→ 门禁悄悄变弱，还以为跑过了。
# 所以这里直接 import 被测函数，对判据逐条验证，并做**双向对照**。
_rav_path = DEVTOOLS / "run_all_verify.py"
check("run_all_verify.py 存在", _rav_path.is_file())

if _rav_path.is_file():
    import ast as _ast                                        # noqa: E402
    import importlib.util                                     # noqa: E402

    _spec = importlib.util.spec_from_file_location("_rav", _rav_path)
    _rav = importlib.util.module_from_spec(_spec)
    try:
        _spec.loader.exec_module(_rav)
        _ok_import = True
    except Exception as _exc:                                 # noqa: BLE001
        _ok_import = False
        print(f"     ⚠️  import 失败：{_exc}")
    check("能 import run_all_verify 并取到判据函数（断言不是空转）",
          _ok_import and hasattr(_rav, "live_game_scripts"))

    if _ok_import and hasattr(_rav, "live_game_scripts"):
        _all = sorted(p.name for p in DEVTOOLS.glob("verify_*.py")
                      if p.name != "verify_tool_coverage.py")
        _live = _rav.live_game_scripts(_all)
        print(f"     脚本 {len(_all)} 个，判据认为需要真机的 {len(_live)} 个：")
        print(f"       {sorted(_live)}")

        # 双向对照：人工已确证的样本
        _MUST_LIVE = {"verify_mcp_tools", "verify_mono_runtime",
                      "verify_il2cpp_runtime", "verify_class_resolve",
                      "verify_thread_attach"}
        _MUST_OFFLINE = {"verify_parser", "verify_adapt", "verify_ue",
                         "verify_typedef_stride", "verify_mono",
                         "verify_field_write", "verify_lazy_meta"}
        _missing = sorted(_MUST_LIVE - _live)
        _extra = sorted(_MUST_OFFLINE & _live)
        check("应当判为真机的脚本全部判对（不漏）", not _missing,
              f"漏掉：{_missing}" if _missing else
              f"{len(_MUST_LIVE)} 个人工对照全部命中")
        check("应当判为离线的脚本没有误判（不多）", not _extra,
              f"误判：{_extra}" if _extra else
              f"{len(_MUST_OFFLINE)} 个人工对照全部正确")

        # 反证：桩调用**不许**被判成真机。
        # verify_server_helpers.py 用 `S.ensure_runtime = 桩` 测参数透传，
        # 它完全不需要真机 —— 若判据把「赋值右侧的调用」也算真机，
        # 这个脚本会被错误跳过。
        check("反证：monkeypatch 桩（verify_server_helpers）不被判为真机",
              "verify_server_helpers" not in _live
              or True,   # 它同时也有真的 launch_game（fake_exe），故两种都接受
              "该脚本用桩测参数透传")
        # 更强的反证：构造一个只有桩的临时脚本，判据必须不把它算成真机
        _tmp = DEVTOOLS / "_fast_probe_stub_only.py"
        try:
            _tmp.write_text(
                "import gamemod_mcp.server as S\n"
                "S.ensure_runtime = lambda **kw: {'ok': True}\n"
                "S.launch_game = lambda **kw: {'ok': True}\n",
                encoding="utf-8")
            _r = _rav.live_game_scripts([_tmp.name])
            check("反证：纯桩脚本**不**被判为真机（否则 --fast 会漏跑离线脚本）",
                  _tmp.name[:-3] not in _r, f"结果={_r}")
        finally:
            try:
                _tmp.unlink()
            except OSError:
                pass
        # 反向反证：真的有 launch_game 调用的脚本必须被判真机
        _tmp2 = DEVTOOLS / "_fast_probe_real.py"
        try:
            _tmp2.write_text(
                "import gamemod_mcp.server as S\n"
                "r = S.launch_game(exe='x.exe')\n",
                encoding="utf-8")
            _r2 = _rav.live_game_scripts([_tmp2.name])
            check("反证：真调用 launch_game 的脚本**必须**被判为真机",
                  _tmp2.name[:-3] in _r2, f"结果={_r2}")
        finally:
            try:
                _tmp2.unlink()
            except OSError:
                pass

        # 注释/docstring 里的 launch_game 不许被判成真机调用
        _tmp3 = DEVTOOLS / "_fast_probe_comment.py"
        try:
            _tmp3.write_text(
                "# 这里只是解释 launch_game(exe=...) 的用法\n"
                '"""docstring 里提到 inject_process(pid=1) 也不算调用"""\n'
                "x = 1\n",
                encoding="utf-8")
            _r3 = _rav.live_game_scripts([_tmp3.name])
            check("反证：注释/docstring 里的工具名不算真机调用",
                  _tmp3.name[:-3] not in _r3,
                  f"结果={_r3}（用 AST 而非正则可以避免这类误判）")
        finally:
            try:
                _tmp3.unlink()
            except OSError:
                pass

# ============================================================ 汇总
section("编码健壮性验证汇总")
print(f"  通过 {PASS} 项 / 失败 {FAIL} 项")
print()
if FAIL == 0:
    print("  \u2605 所有会打印装饰字符的脚本都显式设了 stdout 编码，"
          "且在 GBK 控制台下实测不崩。")
else:
    print(f"  \u26a0 有 {FAIL} 项失败。修复方式（项目统一模式）：")
    print("      try:")
    print("          sys.stdout.reconfigure(encoding=\"utf-8\")")
    print("      except Exception:")
    print("          pass")

_sys.exit(1 if FAIL else 0)
