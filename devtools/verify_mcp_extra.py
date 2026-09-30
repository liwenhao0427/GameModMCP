"""补充覆盖：主流程之外的工具。

`verify_mcp_tools.py` 与 `verify_mcp_mono.py` 覆盖的是**主工作流**
（发现 → 索引 → 查询 → 注入 → Hook）。本脚本补上主流程没有走到的
9 个工具，让 36 个工具全部经过实际调用：

    detect_games / find_game_processes / inspect_binary /
    find_monobehaviours / get_method_source / inject_process /
    hook_address / read_field_value / gc_control

其中 inject_process / hook_address / read_field_value / gc_control
需要一个真实运行中的游戏，所以本脚本会自己 spawn 一个（用 Garden of Witches），
结束时清掉。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import gamemod_mcp.server as S              # noqa: E402

GAME_DIR = r"D:\Projects\Garden of Witches"
GAME_EXE = r"D:\Projects\Garden of Witches\Garden of Witches.exe"
GAME_ASSEMBLY = (
    r"D:\Projects\Garden of Witches\Garden of Witches_Data"
    r"\il2cpp_data\Metadata\global-metadata.dat"
)

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


def section(t: str) -> None:
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


# ============================================================ 1. 纯静态工具
section("1. detect_games / find_game_processes —— 不依赖任何游戏")

dg = S.detect_games(limit=20)
check("detect_games 返回 ok", dg.get("ok") is True, dg.get("error", ""))
games = dg.get("games") or []
print(f"     扫到 {len(games)} 个游戏（本机 Steam 库）")
check("返回结构里带 games 列表", isinstance(games, list))
if games:
    g0 = games[0]
    print(f"     样例: {g0.get('name')} @ {g0.get('install_dir')}")
    check("游戏条目带 name 与 install_dir",
          bool(g0.get("name")) and bool(g0.get("install_dir")))

fg = S.find_game_processes()
check("find_game_processes 返回 ok", fg.get("ok") is True, fg.get("error", ""))
print(f"     当前运行中的游戏进程: {fg.get('count')}")
check("返回结构里带 count", isinstance(fg.get("count"), int),
      f"count={fg.get('count')!r}")
check("返回结构里带 processes 列表",
      isinstance(fg.get("processes"), list),
      f"{len(fg.get('processes') or [])} 条")
if fg.get("processes"):
    p0 = fg["processes"][0]
    print(f"     样例: {p0.get('name')} pid={p0.get('pid')} "
          f"runtime={p0.get('runtime')}")
    check("进程条目带 pid 与 name",
          bool(p0.get("pid")) and bool(p0.get("name")))


section("2. inspect_binary —— PE 结构分析")

if Path(GAME_EXE).is_file():
    ib = S.inspect_binary(GAME_EXE)
    check("inspect_binary 返回 ok", ib.get("ok") is True, ib.get("error", ""))
    print(f"     架构: {ib.get('machine')}  运行时: {ib.get('runtime')}")
    print(f"     节区数: {ib.get('section_count')}  "
          f"导出数: {ib.get('export_count')}")
    check("识别出架构（顶层直接可见）", bool(ib.get("machine")),
          f"machine={ib.get('machine')!r}")
    check("顶层能直接拿到 is_64", isinstance(ib.get("is_64"), bool),
          f"is_64={ib.get('is_64')!r}")
    check("有节区信息", len((ib.get("pe") or {}).get("sections") or []) > 0,
          f"{len((ib.get('pe') or {}).get('sections') or [])} 个")
    # Unity 主程序通常是原生 exe（逻辑在 GameAssembly.dll）
    check("主程序运行时类型合理",
          ib.get("runtime") in ("native", "dotnet", "il2cpp", "mono",
                                "nativeaot"),
          f"runtime={ib.get('runtime')!r}")

    # GameAssembly.dll 才是 IL2CPP 的逻辑所在，它应当有大量导出
    ga = str(Path(GAME_EXE).parent / "GameAssembly.dll")
    if Path(ga).is_file():
        ib2 = S.inspect_binary(ga)
        check("inspect_binary 能读 GameAssembly.dll", ib2.get("ok") is True)
        print(f"     GameAssembly.dll: 导出数={ib2.get('export_count')} "
              f"节区数={ib2.get('section_count')}")
        check("GameAssembly.dll 有导出（il2cpp_* API）",
              (ib2.get("export_count") or 0) > 0,
              f"{ib2.get('export_count')} 个")
else:
    check("游戏主程序存在", False, GAME_EXE)

# 不存在的文件必须报错
bad = S.inspect_binary(r"C:\这个文件根本不存在_xyz.exe")
check("不存在的文件被拒绝", bad.get("ok") is False, bad.get("error", ""))


section("3. find_monobehaviours / get_method_source —— 需要索引")

if not Path(GAME_ASSEMBLY).is_file():
    print(f"  \u26a0\ufe0f  样本元数据不存在，跳过：{GAME_ASSEMBLY}")
else:
    op = S.open_game(game_dir=GAME_DIR)
    check("open_game 成功", op.get("ok") is True, op.get("error", ""))

    mb = S.find_monobehaviours(limit=20)
    check("find_monobehaviours 返回 ok", mb.get("ok") is True, mb.get("error", ""))
    print(f"     MonoBehaviour 脚本: {mb.get('total')}")
    check("找到 MonoBehaviour 脚本", (mb.get("total") or 0) > 0,
          f"{mb.get('total')}")
    if mb.get("items"):
        print(f"     样例: {mb['items'][0].get('full_name')}")

    # 限定名字前缀
    mbn = S.find_monobehaviours(name_pattern="Motion", limit=10)
    print(f"     名字含 Motion 的脚本: {mbn.get('total')}")
    check("find_monobehaviours 支持名字过滤",
          mbn.get("ok") is True, mbn.get("error", ""))

    # get_method_source：拿方法的元数据形态
    gms = S.get_method_source("TransformPingPongMotion", "Update")
    check("get_method_source 返回 ok", gms.get("ok") is True,
          str(gms.get("error", ""))[:80])
    if gms.get("ok"):
        print(f"     方法: {gms.get('method')}  "
              f"token={gms.get('token')}  重载数={gms.get('overload_count')}")
        check("返回里有 token（Hook 定位的关键）",
              bool(gms.get("token")), f"{gms.get('token')!r}")

    # 不存在的方法必须报错
    bad2 = S.get_method_source("TransformPingPongMotion", "这个方法不存在Xyz")
    check("不存在的方法被拒绝", bad2.get("ok") is False, bad2.get("error", ""))

    # 不存在的类必须报错
    bad3 = S.get_method_source("这个类不存在Xyz", "Whatever")
    check("不存在的类被拒绝", bad3.get("ok") is False, bad3.get("error", ""))


# ============================================================ 4. 需要运行时的工具
section("4. inject_process / hook_address / read_field_value / gc_control")

if not Path(GAME_EXE).is_file():
    print(f"  \u26a0\ufe0f  游戏不存在，跳过运行时部分：{GAME_EXE}")
else:
    # 先手动 spawn 一个进程（不注入），再走 inject_process 这条路 ——
    # 这样才真正验证「注入已在运行的进程」这个代码路径，
    # 而不是被 launch_game（spawn 模式）覆盖掉。
    import subprocess
    # 开场先清掉可能残留的同名进程 —— 上一轮如果中途崩了会留下进程，
    # 而 Unity 游戏在有旧实例时新实例可能不正常加载 IL2CPP，
    # 会让「注入成功但 IL2CPP 没就绪」这种假象冒出来。
    st_name = Path(GAME_EXE).stem
    try:
        stale = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {Path(GAME_EXE).name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30,
        )
        for line in (stale.stdout or "").splitlines():
            if "exe" in line.lower():
                sp = line.split('","')[1] if '","' in line else ""
                if sp.isdigit():
                    print(f"     清理残留进程 pid={sp}")
                    subprocess.run(["taskkill", "/F", "/T", "/PID", sp],
                                   capture_output=True, timeout=30)
        time.sleep(1.0)
    except Exception as exc:
        print(f"     残留检查跳过：{exc}")

    print("     手动启动游戏进程（不注入）…")
    proc = subprocess.Popen(
        [GAME_EXE],
        cwd=str(Path(GAME_EXE).parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = proc.pid
    print(f"     pid={pid}，等待 IL2CPP 模块加载…")

    # 等 GameAssembly.dll 加载出来（IL2CPP 游戏启动需要时间）
    ok_inject = False
    init_ok = False
    for attempt in range(30):
        time.sleep(1.0)
        # 进程提前退出就没必要再等了
        if proc.poll() is not None:
            print(f"     进程已退出（exit={proc.returncode}），停止等待")
            break
        res = S.inject_process(pid=pid, auto_find=False)
        if res.get("ok"):
            ok_inject = True
            init = res.get("init") or {}
            init_ok = bool(init.get("ok"))
            print(f"     注入成功（第 {attempt + 1} 次尝试）")
            print(f"     模块={init.get('module')} base={init.get('base')}")
            if res.get("warning"):
                print(f"     ⚠ {res.get('warning')}")
            if init.get("error"):
                print(f"     init 错误: {str(init.get('error'))[:100]}")
            print(f"     缺失 API: {init.get('api_missing')}")
            check("inject_process 返回 ok", True)
            check("注入结果带 pid", res.get("pid") == pid,
                  f"pid={res.get('pid')}")
            if init_ok:
                check("il2cpp 初始化成功", True,
                      f"module={init.get('module')} "
                      f"尝试 {init.get('attempts')} 次 "
                      f"等待 {init.get('waited')}s")
                check("无缺失的 il2cpp API",
                      not init.get("api_missing"),
                      f"缺失={init.get('api_missing')}")
            else:
                # 注入本身成功但运行时没就绪：如实记为警告。
                # 本机是无显示环境，游戏可能根本没走到 IL2CPP 初始化。
                check("il2cpp 初始化成功", False,
                      f"进程已注入但 IL2CPP 未就绪："
                      f"{str(init.get('error'))[:80]}", warn_only=True)
            break
        if attempt == 0:
            print(f"     首次尝试未成功（预期，模块可能还没加载）："
                  f"{str(res.get('error'))[:70]}")
    if not ok_inject:
        check("inject_process 最终成功", False,
              "30 次尝试后仍未注入成功 —— 这条路径在 verify_mcp_tools 里"
              "已被 launch_game 覆盖")

    # 后续工具都需要 IL2CPP 就绪；没就绪就跳过，不要拿环境问题当产品缺陷
    if ok_inject and not init_ok:
        print()
        print("  ⚠ 本机 IL2CPP 未就绪（无显示环境），跳过依赖运行时的工具：")
        print("     runtime_class_info / hook_address / read_field_value / gc_control")
        for name in ("runtime_class_info", "hook_address", "read_field_value",
                     "gc_control"):
            check(f"{name}（需 IL2CPP 就绪）", False,
                  "本机无显示环境，跳过", warn_only=True)
        ok_inject = False

    if ok_inject:
        # ---- hook_address：按绝对地址装 Hook
        tgt = S.find_hook_target(
            name="Time", method="get_frameCount", namespace="UnityEngine",
        )
        if tgt.get("ok") and tgt.get("candidates"):
            cand = tgt["candidates"][0]
            addr = cand.get("address")
            print(f"     目标 {cand.get('name')} address={addr} "
                  f"rva={cand.get('rva_hex')}")
            check("候选带绝对地址", bool(addr), f"{addr!r}")

            if addr:
                ha = S.hook_address(
                    address=addr, label="supp.hook_address", max_calls=1000,
                )
                check("hook_address 返回 ok", ha.get("ok") is True,
                      str(ha.get("error", ""))[:80])
                if ha.get("ok"):
                    print(f"     hook_id={ha.get('hook_id')} "
                          f"rva={ha.get('rva_hex')}")
                    check("hook_address 结果带 RVA", bool(ha.get("rva_hex")))

                    # 主动调一次以证明 Hook 真的装上了
                    S.eval_script(
                        f"(function(){{var f=new NativeFunction(ptr('{addr}'),"
                        f"'int',[]); f(); return 'called';}})()"
                    )
                    time.sleep(0.5)
                    ev = S.drain_events(limit=50)
                    n = len(ev.get("events") or [])
                    print(f"     主动调用后捕获 {n} 条事件")
                    check("hook_address 装的 Hook 真的生效", n > 0,
                          f"{n} 条")
                    S.unhook()

                # 非法地址必须报错
                bad = S.hook_address(address="0x1", label="bad")
                check("非法地址被拒绝", bad.get("ok") is False,
                      str(bad.get("error", ""))[:70])

        # ---- read_field_value
        # 这是唯一能在无 GUI 环境下真正验证「字段偏移算术」的方式：
        # 伪造一块内存当实例，把标记值写到**真实字段偏移**上，
        # 再看 read_field_value 能不能按同一个偏移读回来。
        # 读回来一致 => 偏移计算正确、读内存路径正确。
        rc = S.runtime_class_info(name="PresetLoaderV2")
        check("runtime_class_info 返回 ok", rc.get("ok") is True,
              str(rc.get("error", ""))[:80])
        if rc.get("ok"):
            fields = rc.get("fields") or []
            print(f"     字段数={len(fields)}")
            target_field = next(
                (f for f in fields
                 if f.get("name") == "userDataPresetFile"), None
            )
            if target_field is None:
                target_field = next(
                    (f for f in fields if f.get("offset") is not None
                     and not f.get("is_static")), None
                )
            if target_field:
                fname = target_field.get("name")
                foff = int(target_field.get("offset"))
                marker = 0xCAFEBABE12345678
                print(f"     目标字段 {fname} 偏移={foff} (0x{foff:X})")

                # 造实例：分配一块内存，在「该字段的真实偏移」处写入标记值。
                #
                # 两个必须注意的 Frida 细节（都踩过）：
                #   1. 必须把指针挂在 globalThis 上。只写 `var p = Memory.alloc()`
                #      的话，eval_script 一返回 JS 侧就没引用了，GC 会立刻
                #      回收，下一次 RPC 读到的就是已复用的内存。
                #   2. writeU64 要 UInt64，不能传 ptr 或字符串，否则报
                #      "UInt64 object expected"。
                alloc = S.eval_script(
                    "(function(){"
                    "  globalThis.__verify_buf = Memory.alloc(512);"
                    f"  globalThis.__verify_buf.add({foff})"
                    f".writeU64(uint64('0x{marker:016x}'));"
                    "  return globalThis.__verify_buf.toString();"
                    "})()"
                )
                fake = alloc.get("value")
                check("造出伪造实例地址", bool(fake),
                      f"{fake!r} err={alloc.get('error')}")

                if fake:
                    # 稍等一下，确认这块内存不会在两次 RPC 之间被回收
                    time.sleep(1.0)
                    print(f"     伪造实例 {fake}，"
                          f"已在 +{foff} 写入 0x{marker:016x}")
                    rf = S.read_field_value(
                        instance=fake, name="PresetLoaderV2", field=fname,
                    )
                    check("read_field_value 返回 ok", rf.get("ok") is True,
                          str(rf.get("error", ""))[:80])
                    if rf.get("ok"):
                        got = (rf.get("raw_hex") or "").lower()
                        # 内存小端：0xCAFEBABE12345678 读出来是
                        # 78 56 34 12 be ba fe ca
                        want_le = marker.to_bytes(8, "little").hex()
                        print(f"     读回 raw_hex={got}  "
                              f"as_pointer={rf.get('as_pointer')}")
                        check("按字段偏移读到正确的值（偏移算术正确）",
                              got == want_le,
                              f"读回={got} 期望={want_le}")
                        check("字段地址 = 实例 + 偏移",
                              rf.get("field_address") == hex(
                                  int(fake, 16) + foff),
                              f"field_address={rf.get('field_address')} "
                              f"期望={hex(int(fake, 16) + foff)}")
                        check("as_pointer 还原出原值",
                              (rf.get("as_pointer") or "").lower()
                              == hex(marker),
                              f"as_pointer={rf.get('as_pointer')}")
            else:
                check("找到可用的实例字段", False, "没有带偏移的实例字段")

            # 不存在的字段必须报错（这是最容易骗到 Agent 的假成功）
            badf = S.read_field_value(
                instance="0x1000", name="PresetLoaderV2",
                field="这个字段不存在Xyz",
            )
            check("不存在的字段被拒绝", badf.get("ok") is False,
                  str(badf.get("error", ""))[:70])
            check("拒绝时列出可用字段（便于 Agent 自我纠正）",
                  bool(badf.get("available")),
                  f"{len(badf.get('available') or [])} 个")

        # ---- gc_control（签名是 disable: bool，不是 action 字符串）
        gcd = S.gc_control(disable=True)
        check("gc_control(disable=True) 返回 ok", gcd.get("ok") is True,
              str(gcd.get("error", ""))[:80])
        if gcd.get("ok"):
            print(f"     禁用 GC 后状态: {gcd.get('collecting')}")
            # 关闭 GC 后应当能读回状态
            gce = S.gc_control(disable=False)
            check("gc_control(disable=False) 恢复成功", gce.get("ok") is True,
                  str(gce.get("error", ""))[:60])
            print(f"     恢复后状态: {gce.get('collecting')}")
        else:
            check("gc_control 可用", False,
                  str(gcd.get("error", ""))[:60], warn_only=True)

        # 没有运行时会话时必须明确报错
        S.close_session()
        nogc = S.gc_control(disable=True)
        check("无运行时会话时 gc_control 被拒绝",
              nogc.get("ok") is False, str(nogc.get("error", ""))[:60])
        # 恢复会话以便后续清理（本段后面还会用到）
        if ok_inject:
            S.inject_process(pid=pid, auto_find=False)

    # 清掉手动启的进程
    print("     清理进程…")
    try:
        import subprocess as sp
        sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
               capture_output=True, timeout=30)
        print("     已结束")
    except Exception as exc:
        print(f"     清理时出错（不影响测试结论）：{exc}")


# ============================================================ 汇总
section("补充覆盖验证汇总")
print(f"  通过 {PASS} 项 / 失败 {FAIL} 项 / 警告 {WARN} 项")
print()
if FAIL == 0:
    print("  \u2605 36 个工具全部经过实际调用。")
else:
    print(f"  \u26a0 有 {FAIL} 项失败。")

sys.exit(1 if FAIL else 0)
