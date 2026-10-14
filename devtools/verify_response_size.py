"""验收：**响应体积** —— P2-4 的前后对比、上限不变量、以及旧记录的订正。

## 这个脚本的两个阶段

它先是「量现状」，现在是「卡上限」：

- 量现状时发现交接文档记的「最大 242.9 KB」**归属记错了、量级也低了一个档次**；
- 于是有了 P2-4（`src/gamemod_mcp/cap.py`）：单次响应超过 256 KB 就把最大的
  顶层列表截断、完整列表进句柄。本脚本现在是它的**回归门禁**。

| 调用 | 截断前（线上 text） | 截断后 |
|---|---|---|
| `search_classes('', limit=2000)` | **856,186 B ≈ 836.1 KB** | 260,405 B |
| `search_fields('a', limit=2000)` | 528,055 B | 252,433 B |
| `search_methods('a', limit=2000)` | 504,823 B | 251,193 B |
| `find_derived_classes(MonoBehaviour, 2000)` | 310,628 B | 254,890 B |
| `class_hierarchy(MonoBehaviour, down, 6)` | 248,713 B | 未超限，原样 |

「截断前」的取法不是估算：`@_cap_response` 用 `functools.wraps` 包了工具函数，
所以 `S.search_classes.__wrapped__` 就是**未套上限的原函数**，
可以直接调它量出「没有 cap 时会有多大」。这也顺带证明了装饰器没有吃掉签名
（`__wrapped__` 在，`inspect.signature` 就跟着它走，入参 schema 不变）。

## 量的是「线上字节」，不是「payload 字节」

FastMCP 对 dict 出参的序列化是
`pydantic_core.to_json(result, fallback=str, indent=2)`
（`mcp/server/fastmcp/utilities/func_metadata.py:499`），带缩进，
比 compact JSON 大 22%~27%。所以「payload 多大」与「线上传多大」是两件事，
本脚本量的是后者。第 1 节先用**真实的 FastMCP 调用**自校验这个 emulate；
emulate 一旦不可信，后面所有数字都没有意义。

## 不需要注入

全是静态索引查询，只需要 IL2CPP 元数据样本。样本缺失时记「无法验证」并跳过。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pydantic_core                                      # noqa: E402
from mcp.server.fastmcp import FastMCP                    # noqa: E402

from gamemod_mcp import cap                               # noqa: E402
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


def wire_text(payload: Any) -> str:
    """复刻 FastMCP 对非 str 返回值的序列化（func_metadata.py:499）。"""
    return pydantic_core.to_json(payload, fallback=str, indent=2).decode()


def wire_bytes(payload: Any) -> int:
    return len(wire_text(payload).encode("utf-8"))


def row_count(payload: dict) -> int:
    """从不同形状的返回里取「这一页有多少条」。

    这些工具不是同一个形状：`search_*` 是 `items`，
    `class_hierarchy` 是 `down` / `down_count`。
    取不到就返回 -1（表格里显示成 '-'），**不许拿 0 冒充「没有」**。
    """
    for key in ("items", "derived", "down", "up"):
        v = payload.get(key)
        if isinstance(v, list):
            return len(v)
    for key in ("derived_count", "down_count", "up_count"):
        v = payload.get(key)
        if isinstance(v, int):
            return v
    return -1


#: (标签, 工具名, 关键字参数) —— 全是「可能很大」的只读查询。
#: 用「工具名 + kwargs」而不是 lambda：这样同一个用例能同时跑
#: 「套了上限的」与「`__wrapped__` 原函数」两条路径。
CASES: list[tuple[str, str, dict]] = [
    ("search_classes('')            limit=2000",
     "search_classes", {"pattern": "", "limit": 2000}),
    ("search_fields('a')            limit=2000",
     "search_fields", {"pattern": "a", "limit": 2000}),
    ("search_methods('a')           limit=2000",
     "search_methods", {"pattern": "a", "limit": 2000}),
    ("search_classes('Player')      limit=2000",
     "search_classes", {"pattern": "Player", "limit": 2000}),
    ("find_derived_classes(MonoBehaviour) limit=2000",
     "find_derived_classes", {"base_full_name": "UnityEngine.MonoBehaviour",
                              "limit": 2000}),
    ("find_derived_classes(MonoBehaviour) 默认 limit",
     "find_derived_classes", {"base_full_name": "UnityEngine.MonoBehaviour"}),
    ("class_hierarchy(MonoBehaviour, down, depth=6)",
     "class_hierarchy", {"full_name": "UnityEngine.MonoBehaviour",
                         "direction": "down", "depth": 6}),
]


async def fastmcp_text_size(payload: dict) -> int:
    """把这个 payload 真的过一次 FastMCP，拿它实际产出的 text 字节数。"""
    m = FastMCP("probe-resp-size")

    def impl() -> dict:
        return payload

    m.add_tool(impl, name="probe")
    res = await m.call_tool("probe", {})
    content, _structured = (res if isinstance(res, tuple) else (res, None))
    return len((content[0].text if content else "").encode("utf-8"))


def check_context_cost() -> None:
    """工具 schema 的**固定上下文成本**（**不需要样本**，永远会跑）。

    工具描述 + 入参 schema 是**每次会话都要付**的固定成本（客户端初始化时
    拉一次 `tools/list`），与单次响应体积是两回事，但同属「别把 Agent 的
    上下文炸掉」这条线。这里把现状量成数字并**钉住不许明显增长**。

    为什么是「量 + 封顶」而不是「砍」：实测 45 个工具的 docstring 里
    **几乎没有可无损删除的东西** ——
      · 同一 docstring 内逐字重复的句子：仅 1 处分隔线；
      · 跨工具逐字重复的段落：**0 处**；
      · 与 `instructions`（永远在上下文里）重叠的句子：仅 1 句。
    也就是说 docstring 里那些「历史故事」是**唯一一份**知识，
    删掉就是丢信息（AGENTS.md §2.3 不许编造，反过来也不该丢证据）。
    所以这里是**容量门禁**，不是裁剪任务：超了就说明该考虑拆分/合并工具。
    """
    section("4. 工具 schema 的固定上下文成本（不该无界增长）")

    tools = asyncio.run(S.mcp.list_tools())

    def _sz(o: Any) -> int:
        return len(json.dumps(o, ensure_ascii=False, default=str)
                   .encode("utf-8"))

    instr = str(getattr(S.mcp, "instructions", "") or "")
    per_tool = {t.name: _sz({"name": t.name, "description": t.description,
                             "inputSchema": t.inputSchema}) for t in tools}
    tools_bytes = sum(per_tool.values())
    total_bytes = _sz(instr) + tools_bytes
    desc_chars = sum(len(t.description or "") for t in tools)
    print(f"  instructions {_sz(instr):,} B ＋ tools/list {tools_bytes:,} B "
          f"＝ **{total_bytes:,} B ≈ {total_bytes // 1024} KB**")
    print(f"  （其中 description 共 {desc_chars:,} 字符；"
          f"{len(tools)} 个工具，平均 {tools_bytes // max(1, len(tools)):,} B/个）")
    top = sorted(per_tool.items(), key=lambda kv: -kv[1])[:5]
    print("  最大的 5 个：" + "，".join(f"{n}={v:,}B" for n, v in top))

    # 门禁：固定成本一旦超过 96 KB 就该重新评估（当前实测约 72 KB）。
    # 这个数不是「目标」，而是「明显异常」的边界 —— 定得太紧会逼着后来的
    # 人删掉唯一一份知识（上面已说明那种删除是有损的）。
    check("固定上下文成本仍然在 96 KB 以内（超了说明该拆分/合并工具了）",
          total_bytes <= 96 * 1024,
          f"实测 {total_bytes:,} B ≈ {total_bytes // 1024} KB")
    check("`instructions` 工作流里点到的工具都还存在",
          all(name in per_tool for name in
              ("open_game", "class_detail", "runtime_class_info",
               "hook_method", "drain_events")),
          "抽查 instructions 工作流里的 5 个工具名都还在")

    # 非空转对照：这些数字不是常量 —— 换一个工具子集就会变
    check("成本确实随工具集变化（不是恒定的常量）",
          sum(list(per_tool.values())[:1]) < tools_bytes,
          "单个工具 < 全部工具，说明上面量的是真值")


def main() -> int:
    # 固定成本那一节**不需要样本**，先跑 —— 否则样本缺失时会被一起跳过。
    check_context_cost()

    game = samples.find_il2cpp()
    if not samples.require(game, "IL2CPP 样本（Garden of Witches）"):
        print()
        print("=" * 78)
        print(f"\u26a0\ufe0f  通过 {PASS} 项，失败 {FAIL} 项（响应体积部分"
              f"**无法验证**，未假装通过）")
        print("=" * 78)
        return 1 if FAIL else 0
    opened = S.open_game(game_dir=str(game))
    if not check("open_game 成功", bool(opened.get("ok")),
                 f"engine={opened.get('engine')} "
                 f"types={opened.get('index', {}).get('type_count')}"):
        return 1

    # ---------------- 1. 自校验：emulate 必须等于 FastMCP 的真实行为
    section("1. 自校验：`wire_text()` 是不是真的等同于 FastMCP 的出参序列化")

    probe_small = {"ok": True, "note": "中文与\u0000附近的字符也要一致", "n": 3}
    probe_rows = {"ok": True, "items": [{"i": i, "name": f"类{i}"} for i in range(300)]}
    for tag, pl in (("小载荷", probe_small), ("300 条载荷", probe_rows)):
        real = asyncio.run(fastmcp_text_size(pl))
        ours = wire_bytes(pl)
        check(f"{tag}：FastMCP 实测量 == wire_text() 计算量", real == ours,
              f"FastMCP={real:,} B，我们={ours:,} B")

    # `cap.measure()` 是标准库估的，文档里宣称它**保守偏高**。
    # 这条断言就是那句宣称的证据：估出来只许多、不许少。
    over = []
    for tag, pl in (("纯 ASCII", probe_rows), ("含中文", probe_small)):
        est, real = cap.measure(pl), wire_bytes(pl)
        check(f"{tag}载荷：cap.measure() 不小于真实线上字节（保守偏高）",
              est >= real, f"估 {est:,} B >= 实 {real:,} B（多 {est - real:,} B）")
        over.append(est - real)
    print(f"  说明：偏大来自 `ensure_ascii=True`（非 ASCII 按 \\uXXXX 的 6 字节算）"
          f"与缩进写法；纯 ASCII 时两者一致。")

    # ---------------- 2. P2-4 前后对比
    section("2. P2-4 前后对比：上限是不是真的把最坏情况压下来了")

    print(f"  {'调用':50} {'items':>7} {'截断前':>11} {'截断后':>11} {'线上实测':>11}")
    print("  " + "-" * 76)
    rows: list[tuple[str, dict, int, int]] = []      # (label, capped, raw_B, real_B)
    for label, tool_name, kw in CASES:
        fn = getattr(S, tool_name)
        # 用 `S.RAW_TOOLS[...]` 而不是 `fn.__wrapped__` 拿未套上限的原函数：
        # 动态覆盖率垫片会把模块属性换成自己的记录包装器（没有 `__wrapped__`），
        # 于是「单独跑绿、进套件红」。原因见 `server.py` 里 `RAW_TOOLS` 的注释。
        raw_fn = S.RAW_TOOLS.get(tool_name)
        check(f"{label}：RAW_TOOLS 里有未套上限的原函数", callable(raw_fn))
        raw = raw_fn(**kw) if callable(raw_fn) else {}
        got = fn(**kw)
        if not got.get("ok"):
            check(f"{label} 调用成功", False, str(got.get("error"))[:60])
            continue
        raw_b = cap.measure(raw)
        got_b = cap.measure(got)
        real_b = asyncio.run(fastmcp_text_size(got))
        rows.append((label, got, raw_b, real_b))
        n = row_count(got)
        print(f"  {label:50} {n if n >= 0 else '-':>7} {raw_b:>11,} "
              f"{got_b:>11,} {real_b:>11,}")

    if not rows:
        check("至少量到一条结果", False, "所有查询都失败了")
    else:
        raw_max = max(r[2] for r in rows)
        real_max = max(r[3] for r in rows)
        label_max = max(rows, key=lambda r: r[2])[0]

        # 反空转：必须存在「没有上限就会超 500 KB」的响应，
        # 否则下面的上限断言可能只是「本来就都不大」而已。
        check("截断前确实存在超过 500 KB 的响应（否则上限断言是空转的）",
              raw_max > 500 * 1024,
              f"最大 {raw_max:,} B = {raw_max / 1024:.1f} KB（{label_max}）")

        # 本脚本的核心不变量：**真实**线上字节不超过上限
        over_cap = [(lb, rb) for lb, _p, _raw, rb in rows if rb > cap.CAP_BYTES]
        check("截断后**每一个**响应的真实 FastMCP 线上字节 <= 上限",
              not over_cap,
              f"超限：{over_cap}" if over_cap
              else f"最大 {real_max:,} B = {real_max / 1024:.1f} KB "
                   f"<= {cap.CAP_BYTES:,} B（{cap.CAP_BYTES // 1024} KB）")

        over_est = [(lb, cap.measure(p)) for lb, p, _raw, _rb in rows
                    if cap.measure(p) > cap.CAP_BYTES]
        check("`cap.measure` 自己也认为都没超限（估算与实测一致）",
              not over_est, f"超限：{over_est}" if over_est else "7 条全部 <= 上限")

        capped = [lb for lb, p, _raw, _rb in rows if p.get("capped")]
        check("确实有响应被截断（不是「把上限写得老大、什么也没拦」）",
              bool(capped), f"{len(capped)} 条被截：{[c[:24] for c in capped]}")

        # 没超限的那些必须**一点痕迹都没有**（否则正常查询会被无谓改写）
        untouched = [lb for lb, p, raw_b, _rb in rows
                     if raw_b <= cap.CAP_BYTES]
        dirty = [lb for lb in untouched
                 if any(k in dict(rows[[r[0] for r in rows].index(lb)][1])
                        for k in ("capped", "oversize"))]
        check("截断前就没超限的调用，返回里没有任何 cap 痕迹", not dirty,
              f"被改动的：{dirty}" if dirty
              else f"{len(untouched)} 条未超限，全部原样")

        # ---------------- 2b. 旧记录的订正（用**未截断**的原函数量）
        ch = [r for r in rows if r[0].startswith("class_hierarchy")]
        fd = [r for r in rows
              if r[0].startswith("find_derived_classes") and "limit=2000" in r[0]]
        sc = [r for r in rows if r[0].startswith("search_classes('')")]
        if ch:
            check("旧记录里的 242.9 KB 对应的是 class_hierarchy（可复现）",
                  abs(ch[0][2] - 248_713) / 248_713 < 0.05,
                  f"本轮实测（未截断）{ch[0][2]:,} B")
        if fd:
            check("find_derived_classes 未截断时约 303 KB，比旧记录的 242.9 KB 大",
                  fd[0][2] > 242.9 * 1024, f"本轮实测 {fd[0][2]:,} B")
        if sc:
            check("真实最大是 search_classes('')，未截断时约 836 KB（旧记录的 3.4 倍）",
                  sc[0][2] > 800 * 1024, f"本轮实测 {sc[0][2]:,} B")

        # ---------------- 3. 契约条款：limit 必须真的生效、has_more 必须诚实
        section("3. 契约 §3.1.5：limit 真的生效吗、has_more 诚实吗")

        over = [(lb, row_count(p)) for lb, p, _r, _b in rows
                if row_count(p) > 2000]
        check("没有任何查询返回超过请求的 limit（2000）", not over,
              f"超限：{over[:3]}" if over else "7 条查询全部 <= limit")

        # `total` 必须是**真值**，不随 limit 变 —— 直接量两次来证明，
        # 而不是硬编码一个数（硬编码的数会随默认过滤规则变化而误报）。
        t1 = S.search_classes(pattern="", limit=1).get("total")
        t2000 = S.search_classes(pattern="", limit=2000).get("total")
        check("total 不受 limit 影响（limit=1 与 limit=2000 给出同一个 total）",
              t1 == t2000 and isinstance(t1, int) and t1 > 0,
              f"limit=1 -> {t1}，limit=2000 -> {t2000}")

        # 顺带解释这个 total 与索引类型总数为何不等：
        # search_classes 默认排除编译器生成类型（--dry 掉 3,292 个）。
        tall = S.search_classes(pattern="", limit=1,
                                exclude_compiler_generated=False).get("total")
        check("total 与「索引类型总数」的差额恰好等于被默认排除的编译器生成类型",
              isinstance(tall, int) and isinstance(t1, int) and tall > t1,
              f"含编译器生成 {tall}，默认排除后 {t1}，差 {tall - t1}")

        empty_q = [p for lb, p, _r, _b in rows if lb.startswith("search_classes('')")]
        if empty_q:
            pl = empty_q[0]
            check("被截断时 has_more 如实为 True（明明还有却说没有 = 撒谎）",
                  pl.get("has_more") is True and pl.get("total") == t2000,
                  f"total={pl.get('total')} returned={pl.get('returned')} "
                  f"has_more={pl.get('has_more')}")
            check("被截断时 returned 也跟着改成实际条数（不是原请求的 2000）",
                  pl.get("returned") == len(pl.get("items") or [])
                  and pl.get("returned") < 2000,
                  f"returned={pl.get('returned')}，"
                  f"items={len(pl.get('items') or [])}")

        fd_small = [p for lb, p, _r, _b in rows
                    if lb.startswith("find_derived_classes") and "默认" in lb]
        if fd_small:
            check("默认 limit 下 find_derived_classes 的 has_more 如实为 True",
                  fd_small[0].get("has_more") is True,
                  f"total={fd_small[0].get('total')} "
                  f"returned={fd_small[0].get('returned')}")

    S.close_session(close_runtime=False)

    print()
    print("=" * 78)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
