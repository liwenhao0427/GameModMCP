"""端到端验证：**通过 MCP 工具层**（而不是直接调内部 API）走完整流程。

为什么要这样测
--------------------------------------------------
前面几个验证脚本都是直接 import 内部模块，绕过了 server.py。
但交付物是「给通用 Agent 用的 MCP 服务器」，
真正要保证的是：Agent 通过 MCP 工具调用能拿到正确结果。

所以这里直接调用 server.py 里那些被 @mcp.tool 装饰的函数本体
（FastMCP 装饰器返回的仍是原函数，可直接调用），
验证「工具签名 → 返回值结构 → 数据正确性」整条链路。

覆盖的流程（与真实 Agent 用法一致）：
    1. get_diagnostics          环境自检
    2. inspect_game             识别引擎
    3. open_game                建索引
    4. search_classes           搜索
    5. class_detail             看字段/方法
    6. read_query_result        翻页
    7. launch_game              启动并注入
    8. runtime_info             一致性自检
    9. runtime_class_info       运行时字段偏移 vs 静态元数据
   10. find_hook_target         Hook 落点 + token 比对
   11. hook_method / drain_events / hook_status / unhook
   12. close_session           收尾
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
# 仓库路径由 samples.py 推导（不许硬编码工作区绝对路径）
# 用 os.path 推导，避免依赖后面才 import 的 pathlib
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import samples  # noqa: E402
samples.add_src_to_path()

from gamemod_mcp import server as S  # noqa: E402

# 样本路径：根目录从 samples.py 取，派生路径用相对路径拼出来
_GAME = samples.find_il2cpp()
GAME_DIR = str(_GAME) if _GAME else None
GAME_EXE = str(_GAME / "Garden of Witches.exe") if _GAME else None

# 样本不可用时记为「无法验证」并跳过（跳过不算失败）
if not samples.require(GAME_DIR, "IL2CPP 样本 Garden of Witches"):
    sys.exit(0)
if not os.path.isfile(GAME_EXE):
    print(f"  ⚠️  无法验证：样本主程序不存在 —— {GAME_EXE}")
    sys.exit(0)

PASS, FAIL, WARN = [], [], []


def check(label: str, cond: bool, detail: str = "", warn_only: bool = False):
    tag = "✅" if cond else ("⚠️ " if warn_only else "❌")
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    if cond:
        PASS.append(label)
    elif warn_only:
        WARN.append(label)
    else:
        FAIL.append(label)


def show(title: str):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ================================================================ 1. 诊断
show("1. get_diagnostics —— 环境自检")
d = S.get_diagnostics()
check("get_diagnostics 返回 ok", d.get("ok"), f"version={d.get('version')}")
print(f"     Frida: {d.get('frida', {}).get('installed')} "
      f"v{d.get('frida', {}).get('version')}")
check("IL2CPP 解析器就绪", d["engines"]["il2cpp"]["ready"])
check("Mono 解析器就绪", d["engines"]["mono"]["ready"],
      "mono_meta.py 是否存在", warn_only=True)
check("UE 解析器就绪", d["engines"]["ue"]["ready"],
      "ue.py 是否存在", warn_only=True)
# get_diagnostics 默认不再扫本机进程（耗时约 2.7s，且与 find_game_processes 重复），
# 所以这里改为：确认它**如实说明跳过了**，并验证专门的工具能列出进程。
check("get_diagnostics 如实标注跳过了进程扫描",
      "running_games_skipped" in d,
      "默认不扫本机进程（与 find_game_processes 职责分开）")
gp = S.find_game_processes()
check("find_game_processes 可用（列运行中的游戏）", gp.get("ok") is True,
      f"本机发现 {gp.get('count')} 个")

# ================================================================ 2. 识别引擎
show("2. inspect_game —— 识别游戏引擎")
ig = S.inspect_game(GAME_DIR)
check("inspect_game 返回 ok", ig.get("ok"))
check("识别为 il2cpp", ig.get("engine") == "il2cpp", f"实际={ig.get('engine')}")
print(f"     主程序: {ig.get('main_exe')}")
print(f"     元数据: {ig.get('il2cpp', {}).get('metadata', '')[:70]}...")

# ================================================================ 3. 建索引
show("3. open_game —— 选定游戏并建索引")
og = S.open_game(game_dir=GAME_DIR)
check("open_game 返回 ok", og.get("ok"), og.get("error", ""))
if not og.get("ok"):
    print("  索引失败，后续无法继续"); sys.exit(1)
t0 = time.time()
og2 = S.open_game(game_dir=GAME_DIR)       # 第二次应复用同一会话，不重建
dt2 = time.time() - t0
check("重复 open_game 复用会话（不重建索引）",
      og2.get("ok") and og2.get("reused") is True,
      f"二次耗时 {dt2:.2f}s")
check("二次调用足够快（<1s，说明没重建）", dt2 < 1.0, f"{dt2:.2f}s")
idx_info = og2.get("index") or {}
print(f"     类型={idx_info.get('type_count')} 方法={idx_info.get('method_count')} "
      f"字段={idx_info.get('field_count')} 枚举={idx_info.get('enum_count')}")
check("类型数为 20072", idx_info.get("type_count") == 20072,
      f"实际={idx_info.get('type_count')}")

# ================================================================ 4. 搜索
show("4. search_classes —— 搜索类")
sc = S.search_classes(pattern="UserData_Service")
check("search_classes 返回 ok", sc.get("ok"))
print(f"     命中 {sc.get('total')} 个, 返回 {sc.get('returned')} 个, "
      f"handle={sc.get('handle')}")
names = [i.get("nested_name") or i.get("full_name") for i in sc.get("items", [])]
check("搜到 UserData_Service",
      any("UserData_Service" in (n or "") for n in names),
      f"items={names[:3]}")
check("返回结果带 handle", bool(sc.get("handle")))

mb = S.search_classes(subtype_of="UnityEngine.MonoBehaviour", limit=5)
check("按基类过滤可用", mb.get("ok") and (mb.get("total") or 0) > 1000,
      f"MonoBehaviour 子类 {mb.get('total')} 个")

en = S.search_classes(only_enums=True, pattern="CurrencyKey", limit=5)
check("枚举过滤可用", en.get("ok") and (en.get("total") or 0) >= 1,
      f"命中 {en.get('total')}")

# ================================================================ 5. 类详情
show("5. class_detail —— 字段/方法/继承链")
cd = S.class_detail("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
check("class_detail 返回 ok", cd.get("ok"), cd.get("error", ""))
if cd.get("ok"):
    flds = cd.get("fields") or []
    meths = cd.get("methods") or []
    print(f"     字段 {len(flds)} 个, 方法 {len(meths)} 个, "
          f"父类={cd.get('parent')}")
    check("字段数为 26", len(flds) == 26, f"实际={len(flds)}")
    check("方法数为 50", len(meths) == 50, f"实际={len(meths)}")
    # 字段类型必须被解析出来（这是之前踩过坑的地方）
    typed = [f for f in flds if f.get("type")]
    check("字段类型全部解析成功", len(typed) == len(flds),
          f"{len(typed)}/{len(flds)}")
    store = [m for m in meths if m.get("name") == "get_storeStatSync"]
    check("能找到 get_storeStatSync", bool(store))
    if store:
        print(f"     get_storeStatSync token={store[0].get('token')}")

# 抽查字段类型是否与地面真值一致
pl = S.class_detail("PresetLoaderV2")
if pl.get("ok"):
    pf = {f["name"]: f.get("type") for f in (pl.get("fields") or [])}
    check("PresetLoaderV2.userDataPresetFile 类型 = UnityEngine.TextAsset",
          pf.get("userDataPresetFile") == "UnityEngine.TextAsset",
          f"实际={pf.get('userDataPresetFile')}")

# ================================================================ 6. 句柄翻页
show("6. read_query_result / search_query_result —— 结果句柄")
h = sc.get("handle")
if h:
    p1 = S.read_query_result(h, offset=0, limit=2)
    check("翻页读取成功", p1.get("ok"), p1.get("error", ""))
    if p1.get("ok"):
        check("翻页返回 2 条", p1.get("returned") == 2,
              f"实际={p1.get('returned')}")
    sq = S.search_query_result(h, pattern="User")
    check("结果内过滤成功", sq.get("ok"), sq.get("error", ""))
    bad = S.read_query_result("不存在的句柄-xyz")
    check("无效句柄被正确拒绝", bad.get("ok") is False, bad.get("error", "")[:40])

# ================================================================ 7. 启动注入
show("7. launch_game —— 启动游戏并注入探针")
lg = S.launch_game(exe=GAME_EXE)
check("launch_game 返回 ok", lg.get("ok"), lg.get("error", ""))
pid = None
if lg.get("ok"):
    pid = lg.get("pid")
    if lg.get("warning"):
        print(f"     ⚠️  {lg.get('warning')}")
    init = lg.get("init") or {}
    print(f"     pid={pid} 模块={init.get('module')} base={init.get('base')} "
          f"等待={init.get('waited')}s 尝试={init.get('attempts')}次")
    check("il2cpp 初始化成功（spawn 后轮询等到 DLL 加载）",
          bool(init.get("module")), init.get("error", ""))
    check("无缺失的 il2cpp API", len(init.get("api_missing") or []) == 0,
          f"缺失 {len(init.get('api_missing') or [])} 个")
else:
    print("  注入失败，跳过运行时验证")

sess = S.get_session()
check("get_session 反映已注入", (sess.get("runtime") or {}).get("pid") == pid,
      f"session pid={(sess.get('runtime') or {}).get('pid')} 期望={pid}")

# ================================================================ 8. 运行时一致性
if pid:
    show("8. runtime_info —— 静态/运行时一致性自检")
    ri = S.runtime_info()
    check("runtime_info 返回 ok", ri.get("ok"), ri.get("error", ""))
    if ri.get("ok"):
        print(f"     运行时映像={ri.get('image_count')} "
              f"静态映像={ri.get('static_image_count')} "
              f"一致={ri.get('image_count_matches')}")
        check("映像数一致", ri.get("image_count_matches") is True,
              "证明探针挂到的是同一个游戏实例")
        check("可用导出 59 个", ri.get("api_available") == 59,
              f"实际={ri.get('api_available')}")

    # ============================================================ 9. 运行时类信息
    show("9. runtime_class_info —— 真实字段偏移与方法地址")
    rc = S.runtime_class_info(name="PresetLoaderV2")
    check("runtime_class_info 返回 ok", rc.get("ok"), rc.get("error", ""))
    if rc.get("ok"):
        rt_fields = {f["name"]: f for f in (rc.get("fields") or [])}
        print(f"     字段 {len(rt_fields)} 个, 方法 {len(rc.get('methods') or [])} 个")
        # 核心断言：运行时字段偏移必须与地面真值 dump 一致
        off = (rt_fields.get("userDataPresetFile") or {}).get("offset")
        check("userDataPresetFile 运行时偏移 = 32 (0x20)",
              off == 32, f"实际={off} —— 这是地面真值 dump 里的 /*0x20*/")
        # 所有字段都应有偏移
        with_off = [f for f in rt_fields.values() if f.get("offset") is not None]
        check("全部字段都有运行时偏移", len(with_off) == len(rt_fields),
              f"{len(with_off)}/{len(rt_fields)}")
        # 静态类型 vs 运行时偏移 交叉：userDataPresetFile 静态类型是 TextAsset
        check("静态类型解析与运行时偏移互相印证",
              (rt_fields.get("userDataPresetFile") or {}).get("type")
              == "UnityEngine.TextAsset",
              "静态说它是 TextAsset，运行时说它在 0x20")

    # ============================================================ 10. Hook 落点
    show("10. find_hook_target —— 定位落点并比对 token")
    ft = S.find_hook_target(name="UserData_Service",
                            namespace="TeamTapas.ProjectR.UserDatas.Service",
                            method="get_storeStatSync")
    check("find_hook_target 返回 ok", ft.get("ok"), ft.get("error", ""))
    chosen_token = None
    if ft.get("ok"):
        cands = ft.get("candidates") or []
        print(f"     候选 {len(cands)} 个")
        for c in cands[:3]:
            print(f"       token={c.get('token')} rva={c.get('rva_hex')} "
                  f"in_module={c.get('in_module')} ptr={c.get('pointer')}")
        usable = [c for c in cands if c.get("pointer")]
        check("至少一个候选有真实地址", bool(usable))
        if usable:
            chosen_token = usable[0].get("token")
            # 关键交叉验证：运行时 token 必须等于静态元数据的 token
            static_meth = [m for m in (cd.get("methods") or [])
                           if m.get("name") == "get_storeStatSync"]
            st_tok = static_meth[0].get("token") if static_meth else None
            check("运行时 token == 静态 token",
                  str(chosen_token).lower() == str(st_tok).lower(),
                  f"静态={st_tok} 运行时={chosen_token}")

    # ============================================================ 11. Hook 全流程
    show("11. hook_method / drain_events / hook_status / unhook")
    hm = S.hook_method(
        name="Time", method="get_frameCount",
        namespace="UnityEngine", max_calls=500,
        label="verify.frameCount",
    )
    check("hook_method 返回 ok", hm.get("ok"), hm.get("error", ""))
    hook_id = hm.get("hook_id") if hm.get("ok") else None
    if hook_id:
        ch = hm.get("chosen") or {}
        print(f"     hook_id={hook_id} rva={ch.get('rva_hex')} token={ch.get('token')}")
        check("hook 落点带 RVA", bool(ch.get("rva_hex")))

        # 本机无显示环境，游戏不跑渲染帧，故主动调用以证明 Hook 生效
        ev_before = S.drain_events(limit=10)
        check("drain_events 返回 ok", ev_before.get("ok"))

        S.eval_script(
            "(function(){"
            f"  var f = new NativeFunction(ptr('{ch.get('pointer')}'), 'int', []);"
            "  try { f(); } catch(e) { return 'threw:' + e.message; }"
            "  return 'called';"
            "})()"
        )
        time.sleep(0.5)
        ev = S.drain_events(limit=100)
        enters = [e for e in (ev.get("events") or []) if e.get("phase") == "enter"]
        check("捕获到 Hook 事件", len(enters) >= 1,
              f"{len(enters)} 条 — 证明 Hook 在真实游戏里生效")
        if enters:
            print(f"     事件样例: {json.dumps(enters[0], ensure_ascii=False)[:200]}")

        hs = S.hook_status()
        check("hook_status 返回 ok", hs.get("ok"))
        cnt = (hs.get("hooks") or [{}])[0].get("enter_count")
        check("hook_status 计数 > 0", (cnt or 0) > 0, f"enter_count={cnt}")

        uh = S.unhook(hook_id)
        check("unhook 返回 ok", uh.get("ok"), uh.get("error", ""))

        # 卸载后应不再产生新事件
        S.drain_events(limit=1000)
        S.eval_script(
            "(function(){"
            f"  var f = new NativeFunction(ptr('{ch.get('pointer')}'), 'int', []);"
            "  try { f(); } catch(e) { return 'threw'; }"
            "  return 'called';"
            "})()"
        )
        time.sleep(0.4)
        ev_after = S.drain_events(limit=100)
        after_enters = [e for e in (ev_after.get("events") or [])
                        if e.get("phase") == "enter"]
        check("卸载后不再捕获事件", len(after_enters) == 0,
              f"仍有 {len(after_enters)} 条")

    # ============================================================ 12. 内存与扫描
    show("12. read_memory / write_memory / scan_pattern / eval_script")
    # 用持久缓冲区做读写往返（避免 GC 干扰）
    import gamemod_mcp.engine.state as _st
    buf = _st.state().runtime.alloc_buffer(16)
    check("alloc_buffer 返回 ok", buf.get("ok"), str(buf)[:60])
    if buf.get("ok"):
        addr = buf.get("address")
        w = S.write_memory(addr, "deadbeefcafebabe")
        check("write_memory 返回 ok", w.get("ok"), w.get("error", ""))
        r = S.read_memory(addr, 8)
        check("read_memory 往返一致",
              (r.get("hex") or "").lower() == "deadbeefcafebabe",
              f"写入 deadbeefcafebabe 读回 {r.get('hex')}")
        _st.state().runtime.free_buffer(addr)

    bad = S.read_memory("0x1", 8)
    check("无效地址被正确拒绝", bad.get("ok") is False, bad.get("error", "")[:40])

    sp = S.scan_pattern("4D 5A", max_results=8)
    check("scan_pattern 返回 ok", sp.get("ok"), sp.get("error", ""))
    if sp.get("ok"):
        # 探针返回的字段名是 matched（不是 count）——
        # 注意 IL2CPP 模块内的可读段未必从 PE 头开始，
        # 命中的是模块内出现的 MZ 字节序列，数量因模块而异。
        n = sp.get("matched")
        check("AOB 扫描返回命中数（字段名 matched）", n is not None,
              f"matched={n} 扫描段数={sp.get('scanned_ranges')} "
              f"模块={sp.get('scanned_module')}")

    es = S.eval_script("Process.arch")
    check("eval_script 可用", es.get("ok"), str(es.get("value"))[:40])

    # ============================================================ 13. 错误路径
    show("13. 错误处理 —— 失败必须显式报错，不能假装成功")
    # 未注入时调运行时工具（用一个全新会话不好构造，改为验证参数校验）
    nf = S.class_detail("这个类根本不存在XyzAbc")
    check("不存在的类被正确拒绝", nf.get("ok") is False, nf.get("error", "")[:50])
    nb = S.find_hook_target(name="UserData_Service", method="完全不存在的方法Xyz")
    check("不存在的方法被正确拒绝", nb.get("ok") is False, nb.get("error", "")[:50])
    badh = S.read_query_result("伪造句柄")
    check("伪造句柄被正确拒绝", badh.get("ok") is False)

    # ============================================================ 14. 收尾
    show("14. close_session —— 收尾并还原游戏状态")
    cs = S.close_session()
    check("close_session 返回 ok", cs.get("ok"), cs.get("error", ""))

# ================================================================ 结果
show("端到端结果汇总（通过 MCP 工具层）")
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项 / 警告 {len(WARN)} 项")
if FAIL:
    print("\n  ❌ 失败项：")
    for f_ in FAIL:
        print(f"     - {f_}")
if WARN:
    print("\n  ⚠️  警告项（多为子模块尚未完成或环境限制）：")
    for w in WARN:
        print(f"     - {w}")
if not FAIL:
    print("\n  ★ 通过 MCP 工具层的端到端流程全部走通。")

# 清理 spawn 出来的游戏
if pid:
    print()
    print("  清理：结束游戏进程树…")
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=20)
        print("  已结束")
    except Exception as exc:
        print(f"  结束失败，请手动检查: {exc}")
