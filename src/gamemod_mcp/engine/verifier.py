"""验收条件求值：`wait_for` 与 `verify_state` 共用的同一套语义。

为什么单独成模块
============================================================
`wait_for`（轮询等待）与 `verify_state`（多条件验收）必须对**同一个条件**
给出**同一个结论**。写两份实现必然漂移 —— 那样「同一条件在两处结论不同」
就成了一个新的假信息源（AGENTS.md §3：一个能力只有一个实现）。
把这些求值逻辑收到 `engine/`，`server.py` 只保留工具编排。

注入式依赖（**不要改成 import**）
============================================================
`_eval_condition_inner` 要用 `server.py` 里的两样东西：
  · `RAW_TOOLS`：工具名 → 原函数（绕过响应上限包装）
  · `_screen_mod`：屏幕/日志模块的取用函数

**两者都必须由调用方按参数传入**，不许在本模块里 import：
`devtools/verify_verify_state.py` 会 monkeypatch `S._screen_mod` 与
`S.RAW_TOOLS["..."]`，若这里持有一份自己的引用，那些 monkeypatch
就**不会生效** —— 测试会变成对一个与生产不同的副本求值，
「断言通过」也就不再说明生产代码正确。
"""

from __future__ import annotations

from typing import Any

def _eval_condition(kind: str, *, target: str = "", region: str = "",
                    window: str = "", name: str = "", namespace: str = "",
                    image: str = "", field: str = "", equals: str = "",
                    pattern: str = "", min_count: int = 1,
                    log_baseline: int | None = None, raw_tools: Any = None,
                    screen_mod: Any = None) -> dict:
    """**单次**求值一个验收条件。返回 `{hit, detail, error, unverifiable, baseline}`。

    为什么抽成独立函数：`wait_for`（轮询等待）与 `verify_state`（多条件验收）
    需要**同一套**条件语义。写两份实现必然漂移 —— 那样「同一个条件在两处
    给出不同结论」就会成为一个新的假信息源。AGENTS.md §3 要求一个能力
    只有一个实现。

    这里不做任何循环、不睡、不记证据 —— 那些是调用方的事。
    """
    hit = False
    detail: dict[str, Any] = {}
    error = ""
    unverifiable = ""
    baseline = log_baseline

    try:
        return _eval_condition_inner(
            kind, target=target, region=region, window=window, name=name,
            namespace=namespace, image=image, field=field, equals=equals,
            pattern=pattern, min_count=min_count, baseline=baseline,
            raw_tools=raw_tools, screen_mod=screen_mod)
    except Exception as exc:                                  # noqa: BLE001
        # 单次求值失败**不该**让整个等待崩掉 —— 记进 error 继续轮询。
        return {"hit": False, "detail": detail, "error": str(exc)[:200],
                "unverifiable": "", "baseline": baseline}


def _eval_condition_inner(kind: str, *, target: str = "", region: str = "",
                          window: str = "", name: str = "", namespace: str = "",
                          image: str = "", field: str = "", equals: str = "",
                          pattern: str = "", min_count: int = 1,
                          baseline: int | None = None, raw_tools: Any = None,
                          screen_mod: Any = None) -> dict:
    """`_eval_condition` 的实际实现（外面那层只负责兜异常）。"""
    hit = False
    detail: dict[str, Any] = {}
    error = ""
    unverifiable = ""
    log_baseline = baseline

    if kind == "instances":
        r = raw_tools["find_instances"](
            name=name, namespace=namespace, image=image, max_count=200)
        if r.get("ok"):
            cnt = int(r.get("count") or 0)
            detail = {"count": cnt, "min_count": min_count}
            hit = cnt >= max(1, int(min_count))
        elif r.get("unsupported"):
            # 「本时机不可用」不是「没成立」
            unverifiable = str(r.get("error", ""))[:200]
            detail = {"unsupported": True}
        else:
            error = str(r.get("error", ""))[:200]

    elif kind == "field":
        fi = raw_tools["find_instances"](
            name=name, namespace=namespace, image=image, max_count=8)
        if not fi.get("ok"):
            if fi.get("unsupported"):
                unverifiable = str(fi.get("error", ""))[:200]
                detail = {"unsupported": True}
            else:
                error = str(fi.get("error", ""))[:200]
                detail = {"instances_ok": False}
        else:
            # 实例枚举会**连派生类一起返回**（liveness 语义如此：实测
            # find_instances(Transform) 拿回的全是 RectTransform）。
            # 用基类的字段偏移去读派生类实例可能读错位置，所以：
            #   ① 把 `class_name` 与本类**精确匹配**的实例排在前面；
            #   ② 逐个试（最多 8 个），任何一个命中 `equals` 就算成立。
            # 原来只取 `ptrs[0]`：派生类排在前面时，明明有实例满足条件
            # 却判成不成立 —— 那是**假阴性**，会让 Agent 去改一个没坏的 Mod。
            want_short = (name or "").split(".")[-1].lower()
            insts = list(fi.get("instances") or [])
            exact = [x for x in insts
                     if str(x.get("class_name") or "").split(".")[-1].lower()
                     == want_short]
            ordered = exact + [x for x in insts if x not in exact]
            ptrs = [x.get("pointer") for x in ordered if x.get("pointer")]

            if not ptrs:
                detail = {"count": 0}
            else:
                tried: list[dict] = []
                for p in ptrs[:8]:
                    rf = raw_tools["read_field_value"](
                        instance=p, name=name, field=field,
                        namespace=namespace, image=image)
                    if not rf.get("ok"):
                        tried.append({"pointer": p, "read_ok": False,
                                      "error": str(rf.get("error", ""))[:120]})
                        continue
                    got = _field_value_as_text(rf)
                    tried.append({"pointer": p, "actual": got})
                    if _values_equal(got, equals, rf):
                        # 命中：如实说明是在第几个实例上命中的
                        detail = {"field": field, "actual": got,
                                  "expected": equals, "pointer": p,
                                  "instance_index": len(tried) - 1,
                                  "instances_tried": len(tried),
                                  "exact_class_count": len(exact)}
                        hit = True
                        break
                if not hit:
                    # 一个都没命中：给出**每个**实例的实测值，便于区分
                    # 「值确实不对」与「读的是别的派生类实例」
                    detail = {"field": field, "expected": equals,
                              "instances_tried": len(tried),
                              "exact_class_count": len(exact),
                              "tried": tried}
                    if tried and all(t.get("read_ok") is False for t in tried):
                        error = str(tried[0].get("error") or "")[:200]

    elif kind == "ocr":
        o = raw_tools["ocr"](region=region, window=window, expected=target,
                             match="contains", max_items=50)
        if o.get("ok"):
            n = int(o.get("matched_count") or 0)
            detail = {"matched_count": n, "expected": target,
                      "texts_seen": len(o.get("texts") or [])}
            hit = n > 0
        else:
            error = str(o.get("error", ""))[:200]

    elif kind == "log":
        if not pattern:
            return {"hit": False, "detail": {}, "error": "kind='log' 需要 pattern",
                    "unverifiable": "", "baseline": baseline}
        if not target:
            # target 在 log 分支里是**日志路径**（与 ocr/window 的语义不同）
            return {"hit": False, "detail": {},
                    "error": "kind='log' 时请把日志路径放进 target",
                    "unverifiable": "", "baseline": baseline}
        lr = raw_tools["probe_log"](path=target, pattern=pattern, max_lines=20)
        if lr.get("ok"):
            n = int(lr.get("matched_count") or 0)
            if baseline is None:
                baseline = n
                detail = {"matched_count": n, "pattern": pattern,
                          "baseline": n, "baseline_note": "首次求值记基线，等增量"}
                hit = False
            else:
                detail = {"matched_count": n, "pattern": pattern,
                          "baseline": baseline, "delta": n - baseline}
                hit = n > baseline
        else:
            error = str(lr.get("error", ""))[:200]

    elif kind == "window":
        _screen = screen_mod()
        w = _screen.find_window_rect(target)
        detail = {"window_found": bool(w.get("ok")), "title": w.get("title")}
        hit = bool(w.get("ok"))

    return {"hit": hit, "detail": detail, "error": error,
            "unverifiable": unverifiable, "baseline": baseline}


def _field_value_as_text(rf: dict) -> str:
    """把 `read_field_value` 的结果压成一个可比较的字符串。

    按**实际读取宽度**取值，不给这个宽度下没意义的解释
    （与 `read_field_value` 自己的口径一致）：4 字节字段绝不拼 8 字节。
    """
    w = int(rf.get("read_width") or 8)
    if w == 1:
        if "as_uint8" in rf:
            return str(rf["as_uint8"])
    elif w == 2:
        if "as_uint16" in rf:
            return str(rf["as_uint16"])
    elif w == 4:
        if "as_int32" in rf:
            return str(rf["as_int32"])
    else:
        if "as_int64" in rf:
            return str(rf["as_int64"])
    raw = str(rf.get("raw_hex") or "")
    return raw


def _values_equal(actual: str, expected: str, rf: dict) -> bool:
    """比较字段实际值与期望值。按类型做**语义**比较，不做字符串比较。

    为什么不能只比字符串：`equals="9999"` 与实际的 `as_int32=9999`
    字符串比较是相等的，但 `equals="0x270F"`（同一个数的十六进制）就
    不会相等 —— 而调用方完全可能这么写。浮点也一样（`1.0` vs `1`）。
    """
    if actual is None or expected is None:
        return False
    a, e = str(actual).strip(), str(expected).strip()
    if a == e:
        return True
    ftype = str(rf.get("field_type") or "")
    # 整数族：两边都按整数解析（支持 0x 前缀）
    if ftype and ("Int" in ftype or "Byte" in ftype or "Char" in ftype
                  or "Boolean" in ftype):
        try:
            return int(a, 0) == int(e, 0)
        except (ValueError, TypeError):
            pass
    if ftype in ("System.Single", "System.Double", "float", "double"):
        try:
            return abs(float(a) - float(e)) < 1e-6
        except (ValueError, TypeError):
            pass
    return False
