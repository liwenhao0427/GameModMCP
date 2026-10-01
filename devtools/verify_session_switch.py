#!/usr/bin/env python3
"""验收：**切换游戏时，上一个游戏的身份信息不许留下来**。

背景（实测缺陷，不是推测）
============================================================
`state.select_game()` 在切换游戏时会关掉上一个会话的索引与运行时，
但**不清 `game_dir` / `engine` / `exe` / `metadata_path`**，
而各引擎分支又只写自己那几项：

    IL2CPP 分支：只写 metadata_path
    Mono  分支：写 exe = <X>_Data\\Managed
    UE    分支：写 exe = <游戏>-Win64-Shipping.exe

于是同一进程里按顺序打开

    open_game(Mono 游戏)     -> exe = "...\\Gambonanza_Data\\Managed"
    open_game(IL2CPP 游戏)   -> exe 仍然是 "...\\Gambonanza_Data\\Managed"   ← 串了

这不是显示问题：`exe` 正是 `launch_game` / `ue_profile` / `detect_ue` /
`inspect_binary` 在不传参时默认使用的目标，串了就等于**可能对另一个游戏动手**
（最坏情况：`launch_game()` 启动并注入了上一款游戏）。

为什么以前的验收没发现：**新进程里第一次 open_game 看不出问题** ——
那时 `_state.exe` 本来就是空的，走 `if not _state.exe` 兜底会填对。
只有「一个长驻服务器进程里切换游戏」才会暴露，而这正是 Agent 的用法。

三层验收
============================================================
1. **确定性**：注入陈旧 exe 后强制走切换路径，断言它被清掉；
   并把 `reset_target` 换成空操作做**非空转反证**（旧写法必须报红）。
2. **真实切换**：Mono → IL2CPP → UE → IL2CPP，断言每次的 exe 都属于当前游戏。
3. **进程级端到端**：起真服务器、走 stdio JSON-RPC 握手，
   复现同一条路径（这是 Agent 实际用的方式，不是进程内直调）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
import mcp_stdio                                          # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import gamemod_mcp.server as S                             # noqa: E402
from gamemod_mcp.engine import discovery, state            # noqa: E402

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


def open_with(game_dir: Path) -> dict:
    """走工具层 open_game（缓存指到 .cache-test，不污染用户缓存）。"""
    return S.open_game(game_dir=str(game_dir),
                       cache_dir=str(samples.CACHE_TEST))


def exe_belongs_to(exe: str, game_dir: Path) -> bool:
    """判断 exe 是否落在某个游戏目录里（大小写不敏感）。"""
    if not exe:
        return False
    a = os.path.normcase(os.path.abspath(exe))
    b = os.path.normcase(os.path.abspath(str(game_dir)))
    return a == b or a.startswith(b + os.sep)


# ---------------------------------------------------------------- 1. 确定性

def part_deterministic(il2cpp: Path) -> None:
    section("1. 确定性：陈旧 exe 必须被清掉（含非空转反证）")

    open_with(il2cpp)
    st = state.state()
    check("先建立一个 IL2CPP 会话", bool(st.metadata_path), st.metadata_path[-40:])

    # 让「复用」条件不成立，从而强制走**切换**路径
    st.close_index()
    stale = os.path.join(os.path.abspath(str(il2cpp.parent)), "上一款游戏_Data", "Managed")
    sentinel = os.path.join(os.path.abspath(str(il2cpp)), "本游戏.exe")

    orig_pick = discovery.pick_main_exe
    orig_reset = state.SessionState.reset_target

    def run_case(reset_noop: bool) -> dict:
        """跑一次切换，返回 open_game 的返回。reset_noop=True 模拟修复前的写法。"""
        st.close_index()                      # 确保每次都走切换路径
        st.exe = stale                        # 注入「上一个游戏」的 exe
        discovery.pick_main_exe = lambda _gd, _s=sentinel: _s
        state.SessionState.reset_target = (lambda self: None) if reset_noop else orig_reset
        try:
            return open_with(il2cpp)
        finally:
            discovery.pick_main_exe = orig_pick
            state.SessionState.reset_target = orig_reset

    got = run_case(reset_noop=False)
    check("切换后不再带上一款游戏的 exe",
          got.get("exe") == sentinel,
          f"得到 {got.get('exe')!r}")

    # 非空转反证：把 reset_target 换成空操作（= 修复前的行为），
    # 同一条断言必须**报红**。否则说明这条验收根本没在测东西。
    old = run_case(reset_noop=True)
    check("反证：去掉 reset_target 后旧值确实会串过来（说明上面那条不是空转）",
          old.get("exe") == stale,
          f"旧写法得到 {old.get('exe')!r}")

    # 收尾：把注入的脏值清掉，并让「复用」条件不成立 ——
    # 否则下一节第一次 open_game 会命中复用分支（复用 = 什么都不动），
    # 于是把本节**故意注入的脏值**当成缺陷报出来，冤枉的是测试自己。
    st.close_index()
    st.reset_target()


# ---------------------------------------------------------------- 2. 真实切换

def part_real_switch(il2cpp: Path, mono: Path | None, ue: Path | None) -> None:
    section("2. 真实切换：每次的 exe 都必须属于当前游戏")

    r = open_with(il2cpp)
    check("IL2CPP 会话的 exe 指向自己",
          exe_belongs_to(r.get("exe", ""), il2cpp),
          f"{r.get('exe')}（期望在 {il2cpp} 下）")

    if mono is not None:
        r_m = open_with(mono)
        check("切到 Mono 游戏后 exe 跟着换成它自己的",
              exe_belongs_to(r_m.get("exe", ""), mono),
              f"{r_m.get('exe')}")
        # 再切回 IL2CPP：这里最容易串（IL2CPP 分支不写 exe）
        r2 = open_with(il2cpp)
        check("Mono → IL2CPP 之后 exe 不再是 Mono 的目录",
              not exe_belongs_to(r2.get("exe", ""), mono)
              and exe_belongs_to(r2.get("exe", ""), il2cpp),
              f"{r2.get('exe')}")
    else:
        check("Mono 样本可用", False, "无法验证：样本不在本机", warn_only=True)

    if ue is not None:
        r_u = open_with(ue)
        check("切到 UE 游戏后 exe 是它的 Shipping 程序",
              r_u.get("exe", "").lower().endswith("-win64-shipping.exe"),
              f"{r_u.get('exe')}")
        r3 = open_with(il2cpp)
        check("UE → IL2CPP 之后 exe 不再是 UE 的 Shipping 程序",
              not r3.get("exe", "").lower().endswith("-win64-shipping.exe"),
              f"{r3.get('exe')}")
    else:
        check("UE 样本可用", False, "无法验证：样本不在本机", warn_only=True)


# ---------------------------------------------------------------- 3. 进程级

def part_stdio(il2cpp: Path, mono: Path | None) -> None:
    section("3. 进程级端到端：真服务器 + stdio 握手")

    # 用共享的裸 JSON-RPC 客户端（devtools/mcp_stdio.py）：
    # 这里要验的是「真实 Agent 走 stdio 调用时会不会串游戏」，
    # 与官方 SDK 客户端相比，它还能顺带确认 stdout 没有被污染。
    c = mcp_stdio.McpStdioServer(
        [sys.executable, "-m", "gamemod_mcp.server"],
        env=mcp_stdio.script_form_env(str(samples.SRC), clean_pythonpath=False),
        cwd=str(samples.ROOT)).start()
    try:
        init = c.initialize()
        check("stdio 握手成功（initialize 有 result）",
              init["ok"], init["error"][:200] or
              f"serverInfo={init['server_info']}")

        tl = c.tools()
        check("tools/list 拿到了工具清单", len(tl["names"]) > 20,
              f"{len(tl['names'])} 个工具")

        def call_open(path: Path) -> dict:
            r = c.call_tool("open_game", {"game_dir": str(path),
                                          "cache_dir": str(samples.CACHE_TEST)})
            if not r["ok"]:
                return {"ok": False, "error": r["error"]}
            return r["data"] or {}

        if mono is not None:
            a = call_open(mono)
            check("端到端：先打开 Mono 游戏", a.get("ok") is True, a.get("exe", ""))
            b = call_open(il2cpp)
            check("端到端：随后打开 IL2CPP 游戏，exe 没有串成 Mono 游戏的目录",
                  b.get("ok") is True and not exe_belongs_to(b.get("exe", ""), mono),
                  f"{b.get('exe')}")
            check("端到端：exe 落在当前 IL2CPP 游戏目录下",
                  exe_belongs_to(b.get("exe", ""), il2cpp),
                  f"{b.get('exe')}")
        else:
            check("端到端切换用例", False, "无法验证：Mono 样本不在本机", warn_only=True)

        check("服务器 stdout 全程只有 JSON-RPC（没有多余的打印）",
              c.non_json_lines == 0, "任何一行非 JSON 都会破坏协议")
    finally:
        c.close()


def main() -> int:
    print("验收：切换游戏时上一个游戏的身份信息不许留下来")
    print(f"样本根: {[str(p) for p in samples.SAMPLE_ROOTS]}")

    samples.ensure_cache_dirs()
    il2cpp = samples.find_il2cpp()
    mono = samples.find_mono()
    ue = samples.find_ue_pak_v12()

    if not samples.require(il2cpp, "IL2CPP (Garden of Witches)"):
        print("\n没有 IL2CPP 样本，无法验证。")
        return 1
    print(f"  IL2CPP 样本: {il2cpp}")
    print(f"  Mono   样本: {mono}")
    print(f"  UE     样本: {ue}")

    part_deterministic(il2cpp)
    part_real_switch(il2cpp, mono, ue)
    part_stdio(il2cpp, mono)

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
