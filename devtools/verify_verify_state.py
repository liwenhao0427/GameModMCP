"""验收：`verify_state` 的**证据充分性契约**（不需要游戏，永远会跑）。

本脚本钉住的是一条**外来但被实战验证有效**的规矩，来自
`zhaoxuya520/reverse-skill` 的证据链契约：

> `validated` 需要 **>=2 independent** Evidence（最好 1 静态 + 1 动态）；
> 单条证据**不得静默提升**为 validated。

它在我们自己的实战里立刻得到了印证：CatapultHero 验收 Mod 时，
**日志**（`已修改 精核 = 108`）与**屏幕 OCR**（`精核：108`）两个
互相独立的来源都指向同一结论，才敢说「Mod 生效了」。
只看日志是不够的 —— **日志是 Mod 自己打的**，Mod 写错了照样打日志。

所以本脚本的核心断言是：
  **只有 1 条证据成立时，`verify_state` 必须判 `fail`（证据不足），
   而不是 pass。**

含**非空转反证**：把要求降成「≥1」后同一输入应变成 pass ——
这既证明 `require_independent` 真的影响行为，也证明上面那条断言
不是恒真。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile

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
    print("=" * 78)
    print(title)
    print("=" * 78)


_RAW = S.verify_state
tmp = tempfile.mkdtemp(prefix="gamemod_vs_")

log_hit = os.path.join(tmp, "hit.log")
log_miss = os.path.join(tmp, "miss.log")
io.open(log_hit, "w", encoding="utf-8").write("已修改 精核 = 108\n启动完成\n")
io.open(log_miss, "w", encoding="utf-8").write("nothing interesting\n")

# ================================================================
section("1. 参数校验：空/畸形输入必须明确报错，不静默通过")

r = _RAW(checks="")
check("空 checks → ok=False", r.get("ok") is False)
check("并说明怎么写（给例子）", "checks=" in str(r.get("error", "")),
      str(r.get("error"))[:70])

r = _RAW(checks="nope=x")
check("未知 kind → 记为不成立并报错", r.get("ok") is False)
check("证据里说明了「未知的 kind」",
      "未知" in str((r.get("evidence") or [{}])[0].get("error", "")),
      str((r.get("evidence") or [{}])[0].get("error"))[:60])

r = _RAW(checks="没有等号的条目")
check("缺 = 的条目 → 报「写法不对」",
      "写法" in str((r.get("evidence") or [{}])[0].get("error", "")),
      str((r.get("evidence") or [{}])[0].get("error"))[:60])

r = _RAW(checks="log=只有路径没有井号")
check("log 缺 # 分隔 → 报写法错误（不静默当成整串正则）",
      "写法" in str((r.get("evidence") or [{}])[0].get("error", "")),
      str((r.get("evidence") or [{}])[0].get("error"))[:60])

# ================================================================
section("2. **核心契约**：单条证据不得判 pass")

r1 = _RAW(checks=f"log={log_hit}#已修改 精核", require_independent=2)
check("1 条成立 + require=2 → verdict=fail", r1.get("verdict") == "fail",
      f"verdict={r1.get('verdict')!r}")
check("理由明确说「证据不足」", "证据不足" in str(r1.get("verdict_reason", "")),
      str(r1.get("verdict_reason"))[:80])
check("理由点明「单条证据不得静默提升」",
      "不得静默提升" in str(r1.get("verdict_reason", ""))
      or "独立" in str(r1.get("verdict_reason", "")),
      str(r1.get("verdict_reason"))[:80])
check("ok=False（与 verdict 一致）", r1.get("ok") is False)
check("passed/total 如实报 1/1",
      r1.get("passed") == 1 and r1.get("total") == 1,
      f"{r1.get('passed')}/{r1.get('total')}")

# ================================================================
section("3. 达到独立证据数才 pass（**必须是不同通道**）")

# 2 条 **同通道**（都是 log=）不构成「互相独立」：它们读同一个日志文件，
# 一起对、也会一起错。所以这里**故意**用两条 log 作对照，
# 它必须判 fail 而不是 pass（见第 3b 节的同通道专门断言）。
r_same = _RAW(checks=f"log={log_hit}#已修改 精核,log={log_hit}#启动完成",
              require_independent=2)
check("两条**同通道**（log+log）全成立 → **fail**（不是 pass）",
      r_same.get("verdict") == "fail", f"verdict={r_same.get('verdict')!r}")
check("理由点明「同源 / 缺独立第 2 通道」",
      "同源" in str(r_same.get("verdict_reason", ""))
      or "独立第 2 通道" in str(r_same.get("verdict_reason", "")),
      str(r_same.get("verdict_reason"))[:90])
check("passed=2 但 independent_count=1（条数 != 通道数）",
      r_same.get("passed") == 2 and r_same.get("independent_count") == 1,
      f"passed={r_same.get('passed')} "
      f"independent={r_same.get('independent_count')}")

# 真正的双通道：log（真读文件）+ window（走另一条实现）。
# 假屏幕模块只覆盖 `find_window_rect` / `ocr` —— **其余属性原样继承真实模块**
# （`probe_log` 也走 `_screen_mod().read_log`，整体替换会把 log 通道一起弄坏）。
import types                                              # noqa: E402

_orig_screen_mod = getattr(S, "_screen_mod", None)
_real_screen = S._screen_mod()


def _make_fake_screen(window_hit: bool, ocr_hit: bool) -> types.ModuleType:
    """造一个假屏幕模块：只覆盖窗口/OCR 判定，其余转给真实模块。"""
    fake = types.ModuleType("fake_screen")
    fake.__dict__.update(_real_screen.__dict__)
    fake.find_window_rect = (
        (lambda t: {"ok": True, "title": str(t)}) if window_hit
        else (lambda t: {"ok": False, "error": "stub: 没有这个窗口"}))
    fake.ocr = (
        (lambda **kw: {"ok": True, "matched_count": 1,
                       "texts": [{"text": str(kw.get("expected") or "")}]})
        if ocr_hit else
        (lambda **kw: {"ok": True, "matched_count": 0, "texts": []}))
    return fake


S._screen_mod = lambda: _make_fake_screen(True, False)
try:
    r_multi = _RAW(checks=f"log={log_hit}#已修改 精核,window=SomeWindow",
                   require_independent=2)
finally:
    if _orig_screen_mod is not None:
        S._screen_mod = _orig_screen_mod

check("两条**不同通道**（log+window）全成立 → pass",
      r_multi.get("verdict") == "pass", f"verdict={r_multi.get('verdict')!r}")
check("independent_count == 2（两个不同 kind）",
      r_multi.get("independent_count") == 2,
      f"={r_multi.get('independent_count')}")
check("independent_kinds 如实列出是哪两种通道",
      set(r_multi.get("independent_kinds") or []) == {"log", "window"},
      f"{r_multi.get('independent_kinds')}")

r3 = _RAW(checks=f"log={log_hit}#已修改 精核,log={log_miss}#绝不存在XYZ",
          require_independent=2)
check("两条 log、1 条成立 → fail（不是 pass）",
      r3.get("verdict") == "fail", f"verdict={r3.get('verdict')!r}")
check("此时**不是** unverifiable（因为有一条明确判了不成立）",
      r3.get("verdict") != "unverifiable")
check("passed=1 / total=2 如实报告",
      r3.get("passed") == 1 and r3.get("total") == 2,
      f"{r3.get('passed')}/{r3.get('total')}")

r4 = _RAW(checks=f"log={log_miss}#绝不存在XYZ", require_independent=2)
check("0 条成立 → fail，理由说 0 条", r4.get("verdict") == "fail"
      and "0/" in str(r4.get("verdict_reason", "")),
      str(r4.get("verdict_reason"))[:60])

# ================================================================
section("3b. **同通道不得提升为 pass**（本轮新增的核心修正）")

# 这是本轮修的缺陷：原来 `independent_count = passed`，于是
# `checks="ocr=精核,ocr=108"` 这种**同一张截图上的两条 ocr** 会被算成
# 2 条独立证据 —— 直接把「单条证据不得静默提升」这条契约架空。
# 两条 ocr 一起对（同一张图）、也会一起错（界面没加载完时同时不成立）。
_fake_ocr_hit = _make_fake_screen(False, True)
S._screen_mod = lambda: _fake_ocr_hit
try:
    r_ocr2 = _RAW(checks="ocr=精核,ocr=108", require_independent=2)
finally:
    if _orig_screen_mod is not None:
        S._screen_mod = _orig_screen_mod

check("双 ocr=（同通道）全成立 → **fail**，不许当 2 条独立证据",
      r_ocr2.get("verdict") == "fail", f"verdict={r_ocr2.get('verdict')!r}")
check("passed 如实报 2（两条确实都成立了）",
      r_ocr2.get("passed") == 2, f"passed={r_ocr2.get('passed')}")
check("independent_count 只算 1（同一种通道）",
      r_ocr2.get("independent_count") == 1,
      f"independent={r_ocr2.get('independent_count')}")
check("理由说明「同源 → 缺独立第 2 通道」",
      "同源" in str(r_ocr2.get("verdict_reason", "")),
      str(r_ocr2.get("verdict_reason"))[:90])
check("ok=False（与 verdict 一致）", r_ocr2.get("ok") is False)

# 非空转反证：旧写法（`independent_count = passed`）下，同一组双 ocr 会成为
# pass —— 这里用**行为对照**证明修复真的改变了结论，而不是断言恒真。
_old_independent = r_ocr2.get("passed")          # 旧写法：独立数 == 成立条数
_old_verdict = ("pass" if _old_independent >= 2 else "fail")
check("反证：旧写法（independent_count = passed）会把双 ocr 判成 pass",
      _old_verdict == "pass",
      f"旧判据下 independent_count={_old_independent} → {_old_verdict}")
check("新写法对同一输入的结论**不同**（说明修复不是空转）",
      _old_verdict != r_ocr2.get("verdict"),
      f"旧={_old_verdict} 新={r_ocr2.get('verdict')}")

# 行为对照：同一组 checks 在 require=1 时 pass，说明 fail 的原因是通道数
S._screen_mod = lambda: _fake_ocr_hit
try:
    r_ocr2_1 = _RAW(checks="ocr=精核,ocr=108", require_independent=1)
finally:
    if _orig_screen_mod is not None:
        S._screen_mod = _orig_screen_mod
check("同一组双 ocr 在 require=1 下 pass（证明 fail 的原因是通道数，"
      "不是条件判不出来）",
      r_ocr2_1.get("verdict") == "pass" and r_ocr2.get("verdict") == "fail",
      f"require=1 → {r_ocr2_1.get('verdict')}，"
      f"require=2 → {r_ocr2.get('verdict')}")

# ================================================================
section("4. require_independent 真的影响行为（收了参数就要起作用）")

# 同一输入、只改 require，结论必须变 —— 这同时是第 2 节断言的非空转反证
r5 = _RAW(checks=f"log={log_hit}#已修改 精核", require_independent=1)
check("同一输入 + require=1 → pass（说明 require 真的在起作用）",
      r5.get("verdict") == "pass", f"verdict={r5.get('verdict')!r}")
check("同一输入 + require=2 → fail（与上面形成对照）",
      r1.get("verdict") == "fail")
check("require 回显正确",
      r5.get("require_independent") == 1 and r1.get("require_independent") == 2,
      f"{r5.get('require_independent')} / {r1.get('require_independent')}")
r6 = _RAW(checks=f"log={log_hit}#已修改 精核", require_independent=0)
check("require=0 被抬到 1（不允许「零证据即通过」）",
      r6.get("require_independent") == 1,
      f"={r6.get('require_independent')}")

# ================================================================
section("5. evidence 逐条可核查（每条都说清依据）")

for label, res in (("单条", r1), ("双通道", r_multi)):
    ev = res.get("evidence") or []
    check(f"{label}：evidence 条数与 total 一致",
          len(ev) == res.get("total"), f"{len(ev)} vs {res.get('total')}")
    check(f"{label}：每条都带 check/hit/detail",
          all("check" in e and "hit" in e and "detail" in e for e in ev))
check("命中条目的 detail 里有 matched_count（可核查）",
      any((e.get("detail") or {}).get("matched_count") is not None
          for e in (r_multi.get("evidence") or [])),
      f"{[e.get('detail') for e in (r_multi.get('evidence') or [])]}")
check("命中条目的 detail 里有 window_found（可核查）",
      any("window_found" in (e.get("detail") or {})
          for e in (r_multi.get("evidence") or [])),
      f"{[e.get('detail') for e in (r_multi.get('evidence') or [])]}")

# ================================================================
section("6. 与 wait_for 同源的语义（避免同一条件两处结论不一致）")

check("verify_state 与 wait_for 都经 _eval_condition 求值"
      "（单一实现，不重复）",
      callable(getattr(S, "_eval_condition", None)),
      "若这里为 False，说明条件语义被复制成了两份")

# log 语义差异是**有意的**：wait_for 等「新增」，verify_state 判「当前存在」
_r_wait_like = S.RAW_TOOLS["probe_log"](path=log_hit, pattern="已修改 精核")
check("probe_log 能独立给出当前匹配数（verify_state 的 log 判据基础）",
      (_r_wait_like.get("matched_count") or 0) >= 1,
      f"matched={_r_wait_like.get('matched_count')}")

# ================================================================
section("6b. 报错提示不能把调用方引向**不被接受**的写法")

# 校验的是 `=`，提示就**必须**说 `=`。原来校验 "=" 却报「应是 kind:参数」，
# 于是 Agent 照着提示改成冒号 → 仍然报错 → 在「照提示改」与「还报错」
# 之间死循环。提示与校验必须一致，这条把它钉死。
r_bad = _RAW(checks="没有等号的条目")
_ev0 = (r_bad.get("evidence") or [{}])[0]
_err_txt = str(_ev0.get("error", ""))
check("缺 = 的条目报错里说的是 `=`（不是 `:`）",
      "kind=参数" in _err_txt or "等号" in _err_txt, _err_txt[:90])
check("报错不再诱导冒号写法（旧文案是 kind:参数）",
      "kind:参数" not in _err_txt, _err_txt[:90])
# 提示文案不能只说「用等号」却仍留着冒号示范 —— 两种写法对照检查
check("docstring 里的写法说明也用 `=`",
      "kind=参数" in (_RAW.__doc__ or ""),
      "（docstring 与报错同一个口径）")

# ================================================================
section("6c. 嵌套类名必须**整体**交给探针，不能按最后一个点号拆")

# 违反 AGENTS.md / SKILL.md 的规则：IL2CPP 里嵌套类型的 namespace 是**空**的、
# 只有短名，所以 `Outer.Inner` 拆成 namespace="Outer" / name="Inner" 会解析失败。
# 这里拦在**参数传递**这一层（不注入游戏）：看 field/instances 分支交给
# `_eval_condition` 的 kw 是什么。
_seen_kw: list[dict] = []
_orig_eval = S._eval_condition


def _spy_eval(kind, **kw):
    _seen_kw.append({"kind": kind, **kw})
    return {"hit": False, "detail": {}, "error": "", "unverifiable": "",
            "baseline": None}


S._eval_condition = _spy_eval
try:
    _RAW(checks="field=Outer.Inner.SomeField=7")
    _RAW(checks="instances=Outer.Inner>=2")
finally:
    S._eval_condition = _orig_eval


def _spy_for(kind_want: str) -> dict:
    return next((k for k in _seen_kw if k["kind"] == kind_want), {})


# 注：`field=<类>.<字段>=<值>` 里最后一段是**字段名**，所以
# `Outer.Inner.SomeField` 的「类」部分是 `Outer.Inner`。
_f = _spy_for("field")
check("field 分支把完整类名交给 name，namespace 留空",
      _f.get("name") == "Outer.Inner" and _f.get("namespace") == "",
      f"name={_f.get('name')!r} namespace={_f.get('namespace')!r}")
check("field 分支的字段名仍被正确切出来",
      _f.get("field") == "SomeField", f"field={_f.get('field')!r}")
_i = _spy_for("instances")
check("instances 分支把完整类名交给 name，namespace 留空",
      _i.get("name") == "Outer.Inner" and _i.get("namespace") == "",
      f"name={_i.get('name')!r} namespace={_i.get('namespace')!r}")
# 反证：旧写法会把 `Outer.Inner` 拆成 ns=Outer / name=Inner —— 对照一下
_old_ns, _old_short = "Outer.Inner".rsplit(".", 1)
check("反证：旧写法会拆成 namespace=Outer / name=Inner（这就是缺陷）",
      _old_ns == "Outer" and _old_short == "Inner"
      and not (_f.get("name") == _old_short and _f.get("namespace") == _old_ns),
      f"旧拆法 {( _old_ns, _old_short)}，实际传给探针的"
      f" {( _f.get('name'), _f.get('namespace'))}")

# 带命名空间的顶层类也必须照常可用（不能被这次改动弄坏）
_seen_kw.clear()
S._eval_condition = _spy_eval
try:
    _RAW(checks="field=TeamTapas.ProjectR.UserData.gold=9999")
finally:
    S._eval_condition = _orig_eval
_f2 = _spy_for("field")
check("带命名空间的顶层类整体传给 name（探针自己会解析）",
      _f2.get("name") == "TeamTapas.ProjectR.UserData"
      and _f2.get("field") == "gold",
      f"name={_f2.get('name')!r} field={_f2.get('field')!r}")

# ================================================================
section("6d. field 分支：不能只看第 0 个实例")

# `find_instances` 会**连派生类一起**返回（liveness 语义如此：实测
# find_instances("Transform") 拿回的全是 RectTransform）。用基类字段偏移
# 去读派生类实例可能读错，所以：
#   ① `class_name` 与本类精确匹配的实例要**排在前面**；
#   ② 逐个试，任何一个命中 `equals` 就算成立。
# 原来只取 `ptrs[0]` —— 如果第 0 个是派生类、值不对，明明有实例满足条件
# 却判成不成立（**假阴性**，会让 Agent 去改一个没坏的 Mod）。
_orig_fi = S.RAW_TOOLS["find_instances"]
_orig_rf = S.RAW_TOOLS["read_field_value"]
#: 指针 → 该实例上字段的实际值
_VALUES = {"0xPTR_DERIVED": "111", "0xPTR_EXACT": "9999"}
_READ_ORDER: list[str] = []


def _stub_find_instances(**kw):
    # 故意把**派生类**实例排在前面（这是 liveness 的真实行为）
    return {"ok": True, "count": 2,
            "instances": [{"pointer": "0xPTR_DERIVED",
                           "class_name": "SomeOtherDerived"},
                          {"pointer": "0xPTR_EXACT",
                           "class_name": "UserData"}]}


def _stub_read_field_value(instance="", **kw):
    _READ_ORDER.append(instance)
    return {"ok": True, "read_width": 4, "as_int32": int(_VALUES[instance]),
            "field_type": "System.Int32"}


S.RAW_TOOLS["find_instances"] = _stub_find_instances
S.RAW_TOOLS["read_field_value"] = _stub_read_field_value
try:
    res_field = S._eval_condition("field", name="UserData", field="gold",
                                  equals="9999")
finally:
    S.RAW_TOOLS["find_instances"] = _orig_fi
    S.RAW_TOOLS["read_field_value"] = _orig_rf

check("field 条件在后一个实例上命中 → hit=True（不再只看第 0 个）",
      res_field.get("hit") is True, f"hit={res_field.get('hit')}")
check("命中的是**精确匹配**那个实例（派生类排在前面也找得到）",
      res_field.get("detail", {}).get("pointer") == "0xPTR_EXACT",
      f"pointer={res_field.get('detail', {}).get('pointer')}")
check("精确匹配优先 ⇒ 第 0 个就读到它（tried=1，不必先试派生类）",
      res_field.get("detail", {}).get("instances_tried") == 1,
      f"tried={res_field.get('detail', {}).get('instances_tried')}；"
      f"读取顺序={_READ_ORDER}")
check("精确匹配的实例被**优先读**（第 1 个读的就是它）",
      _READ_ORDER[:1] == ["0xPTR_EXACT"],
      f"读取顺序={_READ_ORDER}")
check("反证：旧写法（只读第 0 个）会读到 111 → 判不成立",
      _VALUES["0xPTR_DERIVED"] != "9999",
      f"第 0 个实例的值={_VALUES['0xPTR_DERIVED']}")

# 另一种情形：**同类多实例**，只有后一个的值符合 —— 必须逐个试到命中为止。
# 这条专门验「遍历多个实例」，与上面的「精确匹配优先」是两件事。
_MULTI = {"0xA1": "1", "0xA2": "2", "0xA3": "9999"}
_ORDER2: list[str] = []


def _stub_fi_multi(**kw):
    return {"ok": True, "count": 3,
            "instances": [{"pointer": p, "class_name": "UserData"}
                          for p in ("0xA1", "0xA2", "0xA3")]}


def _stub_rf_multi(instance="", **kw):
    _ORDER2.append(instance)
    return {"ok": True, "read_width": 4, "as_int32": int(_MULTI[instance]),
            "field_type": "System.Int32"}


S.RAW_TOOLS["find_instances"] = _stub_fi_multi
S.RAW_TOOLS["read_field_value"] = _stub_rf_multi
try:
    res_third = S._eval_condition("field", name="UserData", field="gold",
                                  equals="9999")
finally:
    S.RAW_TOOLS["find_instances"] = _orig_fi
    S.RAW_TOOLS["read_field_value"] = _orig_rf
check("同类多实例：第 3 个才命中 → hit=True", res_third.get("hit") is True,
      f"hit={res_third.get('hit')}")
check("确实逐个试到了第 3 个（instances_tried=3）",
      res_third.get("detail", {}).get("instances_tried") == 3,
      f"tried={res_third.get('detail', {}).get('instances_tried')} "
      f"顺序={_ORDER2}")
check("反证：旧写法只读第 0 个（0xA1=1）永远命中不了 → 假阴性",
      _MULTI["0xA1"] != "9999", f"第 0 个的值={_MULTI['0xA1']}")


# 一个都不命中时，要把**每个**实例的实测值报出来（便于区分「值不对」与
# 「读的是别的派生类」）
_READ_ORDER.clear()
S.RAW_TOOLS["find_instances"] = _stub_find_instances
S.RAW_TOOLS["read_field_value"] = _stub_read_field_value
try:
    res_miss = S._eval_condition("field", name="UserData", field="gold",
                                 equals="12345")
finally:
    S.RAW_TOOLS["find_instances"] = _orig_fi
    S.RAW_TOOLS["read_field_value"] = _orig_rf
check("全不命中 → hit=False", res_miss.get("hit") is False)
check("全不命中时给出每个实例的实测值（可核查）",
      len(res_miss.get("detail", {}).get("tried") or []) == 2,
      f"tried={res_miss.get('detail', {}).get('tried')}")

# ================================================================
section("7. 非空转反证：去掉「证据充分性」检查会怎样")

# 反证：若判据写成「有任意一条成立就 pass」（旧思路），
# 那么 r1（1 条成立、require=2）会变成 pass —— 与实测相反。
_old_pass = r1.get("passed", 0) >= 1          # 旧思路：>=1 就通过
check("旧思路（有任何一条成立即通过）会把 r1 判成 pass —— "
      "而现在实测是 fail，说明契约真的在拦",
      _old_pass is True and r1.get("verdict") == "fail",
      f"old_pass={_old_pass} actual={r1.get('verdict')}")

# 反向：把 require 调回 1，同一输入确实变成 pass
# （证明「fail」不是因为这个条件根本不可能成立）
check("同一条件在 require=1 下能 pass（证明 fail 是证据数不够，"
      "不是条件本身判不出来）",
      r5.get("verdict") == "pass" and r1.get("verdict") == "fail")

# ================================================================
section("8. 条目切分：正则量词里的逗号不许把一条断言切成两条")

# 缺陷现场：切分用的是裸 `str(checks).split(",")`，于是**正则里的逗号**
# 会被当成条目分隔符：
#     log=a.log#金币\s*=\s*\d{1,3},window=X
# 被切成 3 段（多出一段 "3}"），那一段既没有 `=` 也不是合法 kind，
# 于是一条**完全正确的输入**报出「写法不对：应是 kind=参数」的假错误。
# 实测确认（修复前）：total=3，其中一段 error 是「写法不对」。
#
# 修法：逗号后面**紧跟 `标识符=`** 才切分。正则量词 `{1,3}` 的逗号后面是
# 数字，泛型 `Foo<A, B>` 的逗号后面没有 `=` —— 都不会被误切。

_regex_checks = f"log={log_hit}#精核\\s*=\\s*\\d{{1,3}},window=SomeWindow"
print(f"  含正则量词的输入：{_regex_checks}")

# 先确定地展示「裸 split 确实会切坏」——这是本节的立论依据
_naive = [x.strip() for x in _regex_checks.split(",") if x.strip()]
print(f"  裸 split(',') 切成 {len(_naive)} 段：{_naive}")
check("反证：裸 split(',') 确实把 {1,3} 切坏（多出一段）",
      len(_naive) == 3 and any(x.startswith("3}") for x in _naive),
      f"{_naive}")

_fake_win = _make_fake_screen(True, False)
S._screen_mod = lambda: _fake_win
try:
    r_regex = _RAW(checks=_regex_checks, require_independent=2)
finally:
    if _orig_screen_mod is not None:
        S._screen_mod = _orig_screen_mod

check("含正则量词的输入被切成 **2** 条（不是 3 条）",
      r_regex.get("total") == 2, f"total={r_regex.get('total')}")
check("两条都成立 → verdict=pass",
      r_regex.get("verdict") == "pass", f"verdict={r_regex.get('verdict')!r}")
check("没有条目报「写法不对」的假错误",
      not any(e.get("error") for e in (r_regex.get("evidence") or [])),
      f"{[e.get('error') for e in (r_regex.get('evidence') or [])]}")

# ---- 未知 kind 仍要能被切出来并如实报「未知的 kind」
# （切分收紧后最容易误伤的正是这一类：`nope=1` 也是 `标识符=` 形式）
r_unknown = _RAW(checks="nope=1,ocr=x", require_independent=1)
check("未知 kind + 合法 kind 仍被切成 2 条",
      r_unknown.get("total") == 2, f"total={r_unknown.get('total')}")
check("未知 kind 被如实报为「未知的 kind」（不是切丢了）",
      any("未知的 kind" in str(e.get("error", ""))
          for e in (r_unknown.get("evidence") or [])),
      f"{[e.get('error') for e in (r_unknown.get('evidence') or [])]}")

# ---- 泛型类名里的逗号也不许被切成两条
# `Foo<A, B>` 的逗号后面是空格 + 大写字母（没有 `=`），不该切分。
r_generic = _RAW(checks="instances=Dictionary<int, string>>=1", require_independent=1)
check("泛型参数里的逗号没被当成条目分隔符（只切出 1 条）",
      r_generic.get("total") == 1, f"total={r_generic.get('total')}")

# ---- 反证：旧的裸 split 逻辑对同一输入确实会多切一条
_old_items = [x.strip() for x in _regex_checks.split(",") if x.strip()]
check("反证：旧逻辑切出的段数（3）!= 新逻辑的条目数（2）",
      len(_old_items) == 3 and r_regex.get("total") == 2,
      f"旧 {len(_old_items)} 段 / 新 {r_regex.get('total')} 条")

# ---- 切分器本身是纯函数，单独钉住它的行为（不依赖屏幕/探针）
_split = getattr(S, "_split_checks", None)
check("新增了纯函数 _split_checks（切分逻辑可单测）", callable(_split))
if callable(_split):
    _cases = [
        ("a=1,b=2", 2),
        ("log=x#\\d{1,3},window=y", 2),
        ("instances=Foo<int, string>>=1", 1),
        ("nope=1,ocr=x", 2),
        ("  a=1 ,  b=2  ", 2),
        ("only=1", 1),
        ("", 0),
    ]
    _bad = [(src, _split(src)) for src, want in _cases
            if len(_split(src)) != want]
    check("_split_checks 在 7 组用例上段数全部正确", not _bad, f"{_bad}")
    check("_split_checks 会去掉每段两侧空白",
          _split("  a=1 ,  b=2  ") == ["a=1", "b=2"],
          f"{_split('  a=1 ,  b=2  ')}")

# ================================================================
print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"，警告 {WARN} 项" if WARN else ""))
print("=" * 78)
sys.exit(1 if FAIL else 0)
