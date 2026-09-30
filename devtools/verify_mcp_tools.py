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
import sqlite3
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
from gamemod_mcp.engine import state  # noqa: E402

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

# 直接连索引库，供「total 是否真实」这类断言拿**独立于被测代码**的真值。
# 走 COUNT(*) 而不是再调一次被测工具 —— 拿被测实现当真值等于没验证。
_idx_conn = sqlite3.connect(str(state.state().index.db_path))

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

# ---- list_images：MCP 层此前只在会话对象上验证过，工具层没跑过 ----
li = S.list_images()
check("list_images 返回 ok", li.get("ok"), li.get("error", ""))
print(f"     程序集 {li.get('total')} 个, 返回前 {li.get('returned')} 个")
check("程序集数量合理（>10）", (li.get("total") or 0) > 10,
      f"实际={li.get('total')}")
_top = (li.get("items") or [{}])[0]
check("每项含 image 与 type_count",
      bool(_top.get("image")) and _top.get("type_count") is not None,
      f"首项={_top}")
li_f = S.list_images(pattern="TeamTapas")
check("list_images 支持 pattern 过滤",
      li_f.get("ok") and (li_f.get("total") or 0) >= 1
      and all("TeamTapas" in (i.get("image") or "")
              for i in (li_f.get("items") or [])),
      f"命中 {li_f.get('total')} 个（过滤后每项都应含 TeamTapas）")
# 反例：不匹配的 pattern 必须如实返回 0，而不是回退成全量
li_none = S.list_images(pattern="绝对不存在的程序集名_xyz")
check("list_images pattern 无命中时如实返回 0",
      li_none.get("ok") and li_none.get("total") == 0
      and not (li_none.get("items") or []),
      f"total={li_none.get('total')}")

# ---- search_fields / search_methods：total 真值、offset 翻页、owner 消歧 ----
# 这三项都是本轮修复的真实缺陷，断言要点是「对得上真值」，不是「没报错」。
# 真值直接来自索引库的 COUNT(*)，是独立于工具实现的一条路径。
show("4b. search_fields / search_methods —— total 真值 + 翻页 + owner 消歧")

_tp = "a"   # 用极泛的模式把结果数顶到句柄上限之上


def _truth(table: str, pat: str) -> int:
    """直接用 SQL 算真值（绕开被测代码）。"""
    row = _idx_conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE name LIKE ?", (f"%{pat}%",)).fetchone()
    return row[0]


for _tab, _fn, _label in (("fields", S.search_fields, "search_fields"),
                          ("methods", S.search_methods, "search_methods")):
    _truth_n = _truth(_tab, _tp)
    _r = _fn(pattern=_tp, limit=5)
    print(f"     {_label}({_tp!r}): total={_r.get('total')} 真值={_truth_n} "
          f"returned={_r.get('returned')} has_more={_r.get('has_more')}")
    check(f"{_label} 的 total 是真实总数（不是被 LIMIT 截断的值）",
          _r.get("total") == _truth_n,
          f"工具={_r.get('total')} 真值={_truth_n}")
    check(f"{_label} 返回了 returned/offset/limit/has_more",
          all(k in _r for k in ("returned", "offset", "limit", "has_more")))
    check(f"{_label} 结果远多于 limit 时 has_more 必须为 True",
          _r.get("has_more") is True, f"has_more={_r.get('has_more')}")

# total 超过句柄上限时要如实标注（句柄存不下全集，但不能装作存下了）
_big = S.search_fields(pattern=_tp, limit=5)
if (_big.get("total") or 0) > 20000:
    check("总数超过句柄上限时如实标注 handle_truncated",
          _big.get("handle_truncated") is True and bool(_big.get("handle_note")),
          f"handle_truncated={_big.get('handle_truncated')}")

# offset 必须真的生效：两页不能有重叠，且合起来是前 2*limit 条
_p1 = S.search_fields(pattern=_tp, offset=0, limit=10)
_p2 = S.search_fields(pattern=_tp, offset=10, limit=10)
_i1 = [i["index"] for i in _p1.get("items") or []]
_i2 = [i["index"] for i in _p2.get("items") or []]
print(f"     offset 翻页: 第1页 {len(_i1)} 条 / 第2页 {len(_i2)} 条, 重叠="
      f"{len(set(_i1) & set(_i2))}")
check("offset 生效（两页无重叠）", not (set(_i1) & set(_i2)),
      f"交集={sorted(set(_i1) & set(_i2))[:3]}")
check("第二页返回的 offset 被如实回显", _p2.get("offset") == 10,
      f"offset={_p2.get('offset')}")

# 小结果集不该出现 has_more=True（假截断）
_small = S.search_fields(pattern="Effect", limit=2000)
check("小结果集 has_more 为 False（不假报截断）",
      _small.get("has_more") is False,
      f"total={_small.get('total')} returned={_small.get('returned')} "
      f"has_more={_small.get('has_more')}")

# owner 命中多个同名类时必须拒绝。
# 这是本轮修的核心缺陷：原来会把两个无关类（跨程序集）的字段合并返回，
# ok 仍是 true，Agent 据此算偏移必然错。
_amb = S.search_fields(pattern="a", owner="UserData")
print(f"     owner='UserData'（同名类跨程序集）: ok={_amb.get('ok')} "
      f"candidates={len(_amb.get('candidates') or [])}")
check("owner 命中多个同名类时拒绝，不再静默合并",
      _amb.get("ok") is False and len(_amb.get("candidates") or []) > 1,
      f"ok={_amb.get('ok')} candidates={len(_amb.get('candidates') or [])}")
if _amb.get("ok") is False:
    check("拒绝时把候选类摆出来（含程序集，便于消歧）",
          all(c.get("image") for c in _amb.get("candidates") or []),
          f"{[c.get('image') for c in (_amb.get('candidates') or [])]}")
    check("拒绝时给出可操作的 hint",
          bool(_amb.get("hint")), f"{( _amb.get('hint') or '')[:60]}")

# 用 assembly 消歧后必须成功，且**只**返回那个类的字段
_dis = S.search_fields(pattern="a", owner="UserData", assembly="TeamTapas", limit=50)
_seen = sorted({i.get("owner") for i in _dis.get("items") or []})
print(f"     + assembly='TeamTapas': ok={_dis.get('ok')} total={_dis.get('total')} "
      f"类={_seen}")
check("用 assembly 消歧后成功", _dis.get("ok") is True, _dis.get("error", ""))
check("消歧后只返回一个类的字段（不再混入同名类）",
      len(_seen) == 1 and "TeamTapas" in (_seen[0] or ""),
      f"实际涉及 {len(_seen)} 个类：{_seen}")

# 指定了错误的 assembly → 必须报错，不能退回返回那个不属于它的类
_wrong = S.search_fields(pattern="a", owner="UserData", assembly="绝对不存在的程序集")
check("assembly 与 owner 不匹配时报错（不静默返回无关类）",
      _wrong.get("ok") is False, f"ok={_wrong.get('ok')} total={_wrong.get('total')}")

# 完整类名（无歧义）直接成功，且字段确实属于那个类
_full = S.search_fields(pattern="actorData",
                        owner="TeamTapas.ProjectR.UserDatas.UserData", limit=20)
check("完整类名可直接定位（无歧义路径）",
      _full.get("ok") is True and (_full.get("total") or 0) >= 1,
      f"total={_full.get('total')}")
check("命中字段的 owner 与请求的类一致",
      all(i.get("owner") == "TeamTapas.ProjectR.UserDatas.UserData"
          for i in _full.get("items") or []),
      f"{sorted({i.get('owner') for i in _full.get('items') or []})}")

# 不存在的类仍要明确报错
_nc = S.search_fields(pattern="a", owner="这个类不存在Xyz")
check("owner 指向不存在的类时报错", _nc.get("ok") is False,
      str(_nc.get("error"))[:70])
check("报错信息里带上请求的类名（可读性）",
      "这个类不存在Xyz" in str(_nc.get("error")),
      str(_nc.get("error"))[:70])

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

# ---- derived_count 必须是真值（曾写死 limit=200，MonoBehaviour 报 200/实际 1548）----
show("5b. class_detail.derived_count —— 不许被 200 截断后谎报")
cd_mb = S.class_detail("UnityEngine.MonoBehaviour")
check("class_detail(MonoBehaviour) 返回 ok", cd_mb.get("ok"),
      cd_mb.get("error", ""))
if cd_mb.get("ok"):
    dc = cd_mb.get("derived_count")
    print(f"     derived_count={dc} 列表={len(cd_mb.get('derived') or [])} "
          f"has_more={cd_mb.get('derived_has_more')}")
    # 地面真值：本样本 MonoBehaviour 的派生类实测 1548 个
    check("derived_count 是真值 1548（不是被截断的 200）", dc == 1548,
          f"实际={dc} —— 若为 200 说明又被 limit 截断了")
    check("derived 列表被截断时如实标注",
          len(cd_mb.get("derived") or []) == 50
          and cd_mb.get("derived_has_more") is True,
          f"列表={len(cd_mb.get('derived') or [])} "
          f"has_more={cd_mb.get('derived_has_more')}")
    check("derived_returned 与列表长度一致",
          cd_mb.get("derived_returned") == len(cd_mb.get("derived") or []),
          f"returned={cd_mb.get('derived_returned')}")

# ---- 同名类跨程序集：class_detail / class_hierarchy / analyze_dependency 必须拒绝 ----
# 这三个工具返回的是「这个类的全部内容」，选错类不是少几条数据，
# 而是整份字段/方法表都是别的类的，且返回里看不出异常。
# 历史缺陷：它们用 `... LIMIT 1` 静默选中，实测 class_detail("UserData")
# 返回了 Unity.InputSystem.dll 那个（11 字段），而调用方多半要游戏自己的（23 字段）。
show("5c. 同名类跨程序集 —— 必须拒绝而不是静默选一个")

# 先确认真值：索引库里 'UserData' 确实对应 2 个类型
_dup = _idx_conn.execute(
    "SELECT COUNT(*) FROM types WHERE name = 'UserData'").fetchone()[0]
check("前提成立：'UserData' 在索引里对应 2 个同名类", _dup == 2, f"实际={_dup}")

for _fn, _label in ((S.class_detail, "class_detail"),
                    (S.class_hierarchy, "class_hierarchy"),
                    (S.analyze_dependency, "analyze_dependency")):
    _r = _fn(full_name="UserData")
    _c = _r.get("candidates") or []
    print(f"     {_label}('UserData'): ok={_r.get('ok')} candidates={len(_c)}")
    check(f"{_label} 遇同名类时拒绝（不再 LIMIT 1 选一个）",
          _r.get("ok") is False and len(_c) == 2,
          f"ok={_r.get('ok')} candidates={len(_c)}")
    check(f"{_label} 的候选带程序集（可用于 assembly 消歧）",
          all(x.get("image") for x in _c), f"{[x.get('image') for x in _c]}")

# 消歧后必须落到**游戏自己的**那个类，且字段数是它的真值 23
_cd_asm = S.class_detail(full_name="UserData", assembly="TeamTapas")
print(f"     class_detail('UserData', assembly='TeamTapas'): "
      f"ok={_cd_asm.get('ok')} full={_cd_asm.get('full_name')!r} "
      f"字段={len(_cd_asm.get('fields') or [])}")
check("用 assembly 消歧后解析到游戏自己的 UserData",
      _cd_asm.get("ok") is True
      and _cd_asm.get("full_name") == "TeamTapas.ProjectR.UserDatas.UserData",
      f"实际={_cd_asm.get('full_name')!r}")
check("消歧后字段数是该类的真值 23（不是 Unity 那个的 11）",
      len(_cd_asm.get("fields") or []) == 23,
      f"实际={len(_cd_asm.get('fields') or [])} —— 若为 11 说明落到了 Unity 的类")
check("消歧后 image 是游戏程序集",
      "TeamTapas" in (_cd_asm.get("image") or ""),
      f"实际={_cd_asm.get('image')!r}")

# class_hierarchy / analyze_dependency 的消歧路径同样要能走通
_ch_asm = S.class_hierarchy(full_name="UserData", assembly="TeamTapas")
check("class_hierarchy 也能用 assembly 消歧",
      _ch_asm.get("ok") is True
      and (_ch_asm.get("root") or "").startswith("TeamTapas"),
      f"root={_ch_asm.get('root')!r}")
_ad_asm = S.analyze_dependency(full_name="UserData", assembly="TeamTapas")
check("analyze_dependency 也能用 assembly 消歧",
      _ad_asm.get("ok") is True
      and (_ad_asm.get("class") or "").startswith("TeamTapas"),
      f"class={_ad_asm.get('class')!r}")

# 完整类名（无歧义）不应被这次收紧误伤
for _nm in ("TeamTapas.ProjectR.UserDatas.UserData",
            "TeamTapas.ProjectR.UserDatas.Service.UserData_Service",
            "PresetLoaderV2"):
    _r = S.class_detail(full_name=_nm)
    check(f"完整名/唯一短名未被误伤：{_nm}", _r.get("ok") is True,
          str(_r.get("error"))[:60])

# 不存在的类必须仍是明确报错（不能退化成 found=False 的空返回骗过调用方）
_absent = S.class_detail(full_name="这个类不存在Xyz")
check("不存在的类仍明确报错", _absent.get("ok") is False,
      str(_absent.get("error"))[:60])
check("报错信息里保留请求的类名（可读性）",
      "这个类不存在Xyz" in str(_absent.get("error")),
      str(_absent.get("error"))[:60])

# _index_query 的键冲突回归：索引层的 hint 与调用方兜底 hint 同名时
# 曾经直接抛 TypeError（got multiple values for keyword argument 'hint'）
_amb_cd = S.class_detail(full_name="UserData")
check("歧义返回不因 hint 键重名而抛异常（_index_query 回归）",
      isinstance(_amb_cd, dict) and "hint" in _amb_cd,
      f"hint={str(_amb_cd.get('hint'))[:50]}")
check("保留的是索引层更具体的 hint（指向 assembly 消歧）",
      "assembly" in str(_amb_cd.get("hint")),
      f"hint={str(_amb_cd.get('hint'))[:60]}")

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

    # ============================================================ 11b. 重载选择
    show("11b. hook_method 的重载选择 —— 不许盲选第一个候选")
    # 回归用例：曾经 `usable[0]` 盲取第一个候选，会在不知情时挂到错误重载上。
    #
    # 选 System.String.Concat 当靶子（实测运行时已加载、6 个重载且全部可挂）：
    #   token=0x0600026a param=3 / 0x0600026b param=1 / 0x0600026c param=2
    #   token=0x0600026d param=3 / 0x0600026e param=4 / 0x0600026f param=1
    # 它天然覆盖两种情形：param=4 唯一可区分，param=3 与 param=1 各有 2 个
    # → 正好用来验证「能区分时按参数选、不能区分时报歧义」。
    #
    # 教训：最初选了 BodyPart.Visualize（静态元数据里确实有 2 个重载），
    # 但它**运行时没被加载** → find_hook_target 返回「未找到类」。
    # 选靶子必须挑「运行时确实存在」的类，否则测的是环境不是产品。
    OVL_NS, OVL_CLS, OVL_M = "System", "String", "Concat"
    ovl_static = S.class_detail("System.String", method_name=OVL_M,
                                include_inherited=True)
    ovl_meths = ovl_static.get("methods") or []
    print(f"     静态元数据：{OVL_CLS}.{OVL_M} 有 {len(ovl_meths)} 个重载")
    for m in ovl_meths:
        print(f"       token={m.get('token')} 参数={m.get('parameter_count')}")

    rt_ovl = S.find_hook_target(name=OVL_CLS, method=OVL_M, namespace=OVL_NS)
    if not rt_ovl.get("ok"):
        check("运行时能找到 System.String.Concat", False,
              rt_ovl.get("error", "")[:80], warn_only=True)
    else:
        usable = [c for c in (rt_ovl.get("candidates") or [])
                  if c.get("pointer") and c.get("in_module") is not False]
        toks = [str(c.get("token", "")).lower() for c in usable]
        pcs = [c.get("param_count") for c in usable]
        print(f"     运行时候选 {len(rt_ovl.get('candidates') or [])} 个，"
              f"其中可挂 {len(usable)} 个：{list(zip(toks, pcs))}")

        check("System.String.Concat 运行时确有多重载（本用例前提）",
              len(usable) >= 2, f"可挂候选 {len(usable)} 个")

        if len(usable) >= 2:
            # ① 不给选择依据 + 多个候选 → 必须拒绝，而不是猜一个
            amb = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                                max_calls=5, label="verify.ovl.ambig")
            check("多候选且未指定重载 → 拒绝而非盲挂",
                  amb.get("ok") is False and bool(amb.get("error")),
                  f"ok={amb.get('ok')} error={str(amb.get('error'))[:60]}")
            check("拒绝时给出候选清单供 Agent 决策",
                  len(amb.get("candidates") or []) >= 2,
                  f"candidates={len(amb.get('candidates') or [])} 个")

            # ② 按 token 精确选择 —— 首选方式。故意选**最后一个**，
            #    若实现退回「取第一个」这条断言就会失败。
            want = usable[-1]
            want_tok = want.get("token")
            r1 = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                               token=want_tok, max_calls=5,
                               label="verify.ovl.token")
            got1 = (r1.get("chosen") or {}).get("token", "")
            check("按 token 指定重载 → 挂上的就是它（不是第一个）",
                  r1.get("ok") and str(got1).lower() == str(want_tok).lower(),
                  f"想要 {want_tok} 实际 {got1}")
            if r1.get("ok"):
                # 交叉验证：chosen.token 必须能在静态元数据里找到同名重载
                static_tok = {str(m.get("token", "")).lower() for m in ovl_meths}
                check("chosen.token 在静态元数据中存在（静态/运行时对账）",
                      str(got1).lower() in static_tok,
                      f"{got1} ∈ {sorted(static_tok)}")
                S.unhook(r1.get("hook_id"))

            # ③ 按 param_count 选择：挑一个**参数个数唯一**的候选
            uniq_pc = next((p for p in pcs if pcs.count(p) == 1), None)
            if uniq_pc is not None:
                r2 = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                                   param_count=uniq_pc, max_calls=5,
                                   label="verify.ovl.pc")
                got2 = (r2.get("chosen") or {}).get("token", "")
                ch2 = next((c for c in usable
                            if str(c.get("token", "")).lower() == str(got2).lower()),
                           None)
                check(f"按 param_count={uniq_pc} 指定重载 → 参数个数相符",
                      r2.get("ok") and (ch2 or {}).get("param_count") == uniq_pc,
                      f"chosen={got2} param={(ch2 or {}).get('param_count')}")
                if r2.get("ok"):
                    S.unhook(r2.get("hook_id"))

            # ④ 参数个数有歧义 → 必须报歧义并提示改用 token，不许随便挑
            dup_pc = next((p for p in pcs if pcs.count(p) > 1), None)
            if dup_pc is not None:
                r3 = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                                   param_count=dup_pc, max_calls=5,
                                   label="verify.ovl.dup")
                check(f"param_count={dup_pc} 有多个重载 → 报歧义而非任选",
                      r3.get("ok") is False and "token" in str(r3.get("error")),
                      f"ok={r3.get('ok')} err={str(r3.get('error'))[:60]}")

            # ⑤ 不存在的 token → 明确报错，不许随便挂一个
            r4 = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                               token="0xDEADBEEF", max_calls=5,
                               label="verify.ovl.bad")
            check("不存在的 token 被拒绝", r4.get("ok") is False,
                  str(r4.get("error"))[:70])

    # ⑥ 单一候选不受影响（Time.get_frameCount 无重载，走简单路径）
    r5 = S.hook_method(name="Time", method="get_frameCount",
                       namespace="UnityEngine", max_calls=5,
                       label="verify.ovl.single")
    check("无重载方法仍可直接挂（简单路径未被破坏）",
          r5.get("ok"), r5.get("error", "")[:60])
    if r5.get("ok"):
        S.unhook(r5.get("hook_id"))

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
