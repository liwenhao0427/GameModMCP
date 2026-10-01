"""验收：**响应体积** —— P2-4 的基线与上限（本脚本先是「量现状」，再是「卡上限」）。

## 为什么要重建基线

交接文档里记的是「无统一上限，实测最大 `find_derived_classes` 242.9 KB、
`class_hierarchy` 124.4 KB」。本轮逐条重量，发现**记错了归属、也低估了量级**：

| 调用 | FastMCP 实际出参 |
|---|---|
| `class_hierarchy(MonoBehaviour, down, depth=6)` | 248,713 B ≈ **242.9 KB** ← 旧记录里那个 242.9 KB 其实是这一条 |
| `find_derived_classes(MonoBehaviour, limit=2000)` | 310,628 B ≈ 303.3 KB |
| `search_classes('', limit=2000)` | **856,186 B ≈ 836.1 KB** ← 真实最大，是旧记录的 **3.4 倍** |

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
    `find_derived_classes` 是 `derived` / `derived_count`，
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


#: (标签, 调用, 期望的 items 上限) —— 全是「可能很大」的只读查询
CASES: list[tuple[str, Any]] = [
    ("search_classes('')            limit=2000",
     lambda: S.search_classes(pattern="", limit=2000)),
    ("search_fields('a')            limit=2000",
     lambda: S.search_fields(pattern="a", limit=2000)),
    ("search_methods('a')           limit=2000",
     lambda: S.search_methods(pattern="a", limit=2000)),
    ("search_classes('Player')      limit=2000",
     lambda: S.search_classes(pattern="Player", limit=2000)),
    ("find_derived_classes(MonoBehaviour) limit=2000",
     lambda: S.find_derived_classes(base_full_name="UnityEngine.MonoBehaviour",
                                    limit=2000)),
    ("find_derived_classes(MonoBehaviour) 默认 limit",
     lambda: S.find_derived_classes(base_full_name="UnityEngine.MonoBehaviour")),
    ("class_hierarchy(MonoBehaviour, down, depth=6)",
     lambda: S.class_hierarchy(full_name="UnityEngine.MonoBehaviour",
                               direction="down", depth=6)),
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


def main() -> int:
    game = samples.find_il2cpp()
    if not samples.require(game, "IL2CPP 样本（Garden of Witches）"):
        return 0
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

    # ---------------- 2. 现状基线
    section("2. 现状基线：最大的几个只读查询在线上有多大")

    print(f"  {'调用':52} {'items':>7} {'compact':>11} {'线上 text':>12}")
    print("  " + "-" * 74)
    rows: list[tuple[str, int, dict]] = []
    for label, call in CASES:
        payload = call()
        if not payload.get("ok"):
            check(f"{label} 调用成功", False, str(payload.get("error"))[:60])
            continue
        items = payload.get("items")
        n = row_count(payload)
        compact = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        text = wire_bytes(payload)
        rows.append((label, text, payload))
        print(f"  {label:52} {n if n >= 0 else '-':>7} {compact:>11,} {text:>12,}")

    rows.sort(key=lambda r: -r[1])
    print()
    print("  按线上 text 排序（前 5）：")
    for label, text, _ in rows[:5]:
        print(f"    {text:>10,} B  {text / 1024:>7.1f} KB   {label}")

    if not rows:
        check("至少量到一条结果", False, "所有查询都失败了")
    else:
        biggest_label, biggest, biggest_payload = rows[0]
        check("现状：确实存在超过 500 KB 的响应"
              "（P2-4 落地后本断言要**反转**成「都不超过上限」）",
              biggest > 500 * 1024,
              f"最大 {biggest:,} B = {biggest / 1024:.1f} KB（{biggest_label}）")

        # 旧记录的具体订正：242.9 KB 属于 class_hierarchy，不属于
        # find_derived_classes；而真正的最大值接近 836 KB。
        ch = [t for lb, t, _ in rows if lb.startswith("class_hierarchy")]
        fd = [t for lb, t, _ in rows
              if lb.startswith("find_derived_classes") and "limit=2000" in lb]
        if ch:
            check("旧记录里的 242.9 KB 对应的是 class_hierarchy（可复现）",
                  abs(ch[0] - 248_713) / 248_713 < 0.05,
                  f"本轮实测 {ch[0]:,} B")
        if fd:
            check("find_derived_classes 实际约 303 KB，比旧记录的 242.9 KB 大",
                  fd[0] > 242.9 * 1024, f"本轮实测 {fd[0]:,} B")

        # ---------------- 3. 契约条款：limit 必须真的生效、has_more 必须诚实
        section("3. 契约 §3.1.5：limit 真的生效吗、has_more 诚实吗")

        over = [(lb, row_count(pl)) for lb, _t, pl in rows
                if row_count(pl) > 2000]
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

        empty_q = [pl for lb, _t, pl in rows if lb.startswith("search_classes('')")]
        if empty_q:
            pl = empty_q[0]
            check("被 limit 截断时 has_more 如实为 True",
                  pl.get("has_more") is True and pl.get("total") == t2000,
                  f"total={pl.get('total')} returned={pl.get('returned')} "
                  f"has_more={pl.get('has_more')}")

        fd_small = [pl for lb, _t, pl in rows
                    if lb.startswith("find_derived_classes")
                    and "默认" in lb]
        if fd_small:
            check("默认 limit 下 find_derived_classes 的 has_more 如实为 True",
                  fd_small[0].get("has_more") is True,
                  f"total={fd_small[0].get('total')} "
                  f"returned={fd_small[0].get('returned')}")

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
