"""索引性能基准：把「开索引」与「每次查询」的成本拆开量清楚。

为什么需要这个
============================================================
`docs/项目梳理与优化建议.md` 的 P1-2 / P1-3 都是**性能**改动，而性能改动
最容易变成「我觉得快了」。本项目的一贯要求是「数字必须是跑出来的」，
所以改之前先量、改之后再量，两次都要有留痕。

它量三件事，对应三个不同的优化方向：

1. **开索引**（P1-2）—— `Index.open` 命中缓存时仍然全量解析元数据。
   这里把 `_load_meta` / 缓存命中 / 强制重建 / 纯 SQL 下限分开量，
   差额就是「不加载解析器」能省下的部分。
2. **查询**（P1-3）—— `search_*` 会把**全量**结果解析一遍塞进句柄，
   与 `limit` 无关。这里把 ① SQL 取回 ② 解析这一页 ③ 解析全部进句柄
   分开量，用来判断「惰性句柄」值不值得做。
3. **继承过滤**（P1-4）—— 核实文档里 `subtype_of` 的耗时说法。

用法：
    python devtools/bench_index.py              # 两个引擎都跑
    python devtools/bench_index.py --engine il2cpp

注意：本脚本是**基准**不是**验收**，所以不叫 `verify_*`，
也不进 `run_all_verify.py` 的清单 —— 它没有「通过/失败」，
只有实测数字。数字随机器而变，不要把它当成回归门禁。
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
import time
from pathlib import Path

# 中文控制台默认 GBK，emoji 会直接抛 UnicodeEncodeError。
# 全项目 20 个脚本里有 18 个都做了这件事，本脚本照做。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
if not isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import samples  # noqa: E402

samples.add_src_to_path()
from gamemod_mcp.engine import index as I  # noqa: E402
from gamemod_mcp.engine import pe, ue  # noqa: E402

#: 每个查询重复多少次取最小值（排除首次预热与系统抖动）
REPEATS = 5


def ms(t0: float, t1: float | None = None) -> float:
    return ((t1 if t1 is not None else time.perf_counter()) - t0) * 1000


def best(fn):
    """跑 REPEATS 次取最小耗时，同时返回最后一次的结果。"""
    ts = []
    result = None
    for _ in range(REPEATS):
        t0 = time.perf_counter()
        result = fn()
        ts.append(ms(t0))
    return min(ts), result


CASES = [
    ("il2cpp", "IL2CPP",
     lambda: samples.find_il2cpp(),
     "Garden of Witches_Data/il2cpp_data/Metadata/global-metadata.dat"),
    ("mono", "Mono",
     lambda: samples.find_mono(),
     "Archon Soul_Data/Managed/Assembly-CSharp.dll"),
]


def bench_open(label: str, mpath: Path) -> None:
    print("-" * 76)
    print(f"[P1-2] 开索引成本  ——  {label}")
    print("-" * 76)

    t0 = time.perf_counter()
    meta = I.Index._load_meta(mpath, "")
    t1 = time.perf_counter()
    n_types = len(meta.types)
    meta.close()
    print(f"  _load_meta 全量解析          {ms(t0, t1):9.1f} ms"
          f"   类型 {n_types:,}")

    t1 = time.perf_counter()
    idx = I.Index.open(mpath)
    t2 = time.perf_counter()
    db = idx.db_path
    print(f"  Index.open 缓存命中          {ms(t1, t2):9.1f} ms"
          f"   cache_hit={idx.cache_hit}")
    idx.close()

    t2 = time.perf_counter()
    idx2 = I.Index.open(mpath, force_rebuild=True)
    t3 = time.perf_counter()
    print(f"  Index.open 强制重建          {ms(t2, t3):9.1f} ms")
    idx2.close()

    # 纯 SQL 下限：校验 + 连库，不碰解析器
    t3 = time.perf_counter()
    valid = I.Index._is_valid(db)
    t4 = time.perf_counter()
    conn = sqlite3.connect(str(db))
    conn.execute("SELECT COUNT(*) FROM types").fetchone()
    t5 = time.perf_counter()
    conn.close()
    print(f"  _is_valid（含一次 connect）  {ms(t3, t4):9.1f} ms   valid={valid}")
    print(f"  connect + COUNT(*)           {ms(t4, t5):9.1f} ms")
    print(f"  → 缓存命中里可省的份额 ≈ "
          f"{ms(t0, t1) / max(ms(t1, t2), 1e-9) * 100:.0f}%（P1-2 的目标）")
    print(f"  → 缓存命中的理论下限 ≈ {ms(t3, t5):.1f} ms"
          f"（校验 + 连库，不碰解析器）")


def bench_query(label: str, mpath: Path) -> None:
    print("-" * 76)
    print(f"[P1-3] 单次查询成本  ——  {label}")
    print("-" * 76)
    idx = I.Index.open(mpath)

    for tag, sql in (
        ("全表 pattern=''", "SELECT * FROM types ORDER BY nested_name"),
        ("pattern='Player'",
         "SELECT * FROM types WHERE name LIKE '%Player%' ORDER BY nested_name"),
    ):
        t0 = time.perf_counter()
        rows = list(idx.conn.execute(sql))
        t1 = time.perf_counter()
        [idx._row_to_type(r) for r in rows[:20]]
        t2 = time.perf_counter()
        [idx._row_to_type(r) for r in rows]
        t3 = time.perf_counter()
        total = ms(t0, t3)
        print(f"  {tag}   行数 {len(rows):,}")
        print(f"    ① SQL 取回                 {ms(t0, t1):9.1f} ms")
        print(f"    ② 解析这一页（20 条）      {ms(t1, t2):9.1f} ms")
        print(f"    ③ 解析全部进句柄           {ms(t2, t3):9.1f} ms"
              f"   ← 与 limit 无关")
        print(f"    合计                       {total:9.1f} ms"
              f"   （③ 占 {ms(t2, t3) / max(total, 1e-9) * 100:.0f}%）")

    print("-" * 76)
    print(f"[P1-4] 继承过滤成本  ——  {label}")
    print("-" * 76)
    plain, a = best(lambda: idx.search_classes("", limit=20))
    sub, b = best(lambda: idx.search_classes(
        "", limit=20, subtype_of="UnityEngine.MonoBehaviour"))
    print(f"  pattern=''                 {plain:9.1f} ms  命中 {a['total']:,}")
    print(f"  pattern='' + subtype_of    {sub:9.1f} ms  命中 {b['total']:,}")
    print(f"  → 继承过滤净增 {sub - plain:+.1f} ms"
          f"（文档 §4.8 称「秒级」，此处以实测为准）")

    print("-" * 76)
    print(f"[参考] 各查询耗时（{REPEATS} 次取最小）  ——  {label}")
    print("-" * 76)
    for name, fn in (
        ("search_classes('Player')", lambda: idx.search_classes("Player", limit=20)),
        ("search_classes('')", lambda: idx.search_classes("", limit=20)),
        ("search_fields('a')", lambda: idx.search_fields("a", limit=20)),
        ("search_methods('a')", lambda: idx.search_methods("a", limit=20)),
        ("find_derived_classes(MonoBehaviour)",
         lambda: idx.find_derived_classes("UnityEngine.MonoBehaviour")),
        ("class_hierarchy(down, depth=1)",
         lambda: idx.class_hierarchy("UnityEngine.MonoBehaviour",
                                     direction="down", depth=1)),
    ):
        dt, _ = best(fn)
        print(f"  {name:<42} {dt:9.1f} ms")

    idx.close()


def bench_dup_parse() -> None:
    """P1-5：重复解析 —— 同一次调用里把同一份数据解析两遍。"""
    print()
    print("=" * 76)
    print("[P1-5] 重复解析成本")
    print("=" * 76)

    print("-" * 76)
    print("① inspect_binary：PE 被解析几遍")
    print("-" * 76)
    game = samples.find_il2cpp()
    if game is None:
        print("  IL2CPP 样本不在本机，无法验证（跳过）")
    else:
        exe = samples.game_exe(game)
        print(f"  样本 {exe.name}")
        n = 5
        t0 = time.perf_counter()
        for _ in range(n):
            pe.parse(exe)
        t1 = time.perf_counter()
        for _ in range(n):
            pe.summarize(exe)
        t2 = time.perf_counter()
        p_ms = ms(t0, t1) / n
        s_ms = ms(t1, t2) / n
        print(f"  pe.parse       单次 {p_ms:7.2f} ms")
        print(f"  pe.summarize   单次 {s_ms:7.2f} ms"
              f"   ← 内部又 parse 了一遍")
        print(f"  → state.inspect_binary 若两者都调，就付了约 "
              f"{p_ms + s_ms:.2f} ms，其中 {p_ms:.2f} ms 是重复的")

    print("-" * 76)
    print("② ue_list_entries：容器索引被解析几遍")
    print("-" * 76)
    for label, finder in (("pak v12 (CatapultHero)", samples.find_ue_pak_v12),
                          ("pak v11 (Dinoblade)", samples.find_ue_pak_v11)):
        g = finder()
        if g is None:
            print(f"  {label}: 样本不在本机，无法验证（跳过）")
            continue
        utocs = sorted(g.rglob("*.utoc"))
        paks = sorted(g.rglob("*.pak"))
        print(f"  {label}:  pak={len(paks)}  utoc={len(utocs)}")
        for p in (utocs[:1] + paks[:1]):
            is_utoc = p.suffix.lower() == ".utoc"
            t0 = time.perf_counter()
            if is_utoc:
                r1 = ue.read_iostore_index(str(p))
            else:
                r1 = ue.read_pak_index(str(p))
            t1 = time.perf_counter()
            if is_utoc:
                entries = ue.list_iostore_entries(str(p))
            else:
                entries = ue.list_pak_entries(str(p))
            t2 = time.perf_counter()
            first = ms(t0, t1) / 1000
            both = ms(t0, t2) / 1000
            print(f"    {p.name}  ({p.stat().st_size / 1024 / 1024:.1f} MB,"
                  f" 条目 {len(entries):,})")
            print(f"      read_*_index    {first:6.3f}s  ok={r1.get('ok')}")
            print(f"      list_*_entries  {ms(t1, t2) / 1000:6.3f}s")
            print(f"      → server.py 里两次都调 = {both:.3f}s，"
                  f"其中 {first:.3f}s（{first / max(both, 1e-9) * 100:.0f}%）"
                  f"是重复解析")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="", choices=["", "il2cpp", "mono"],
                    help="只跑某一个引擎（默认两个都跑）")
    ap.add_argument("--only-dup", action="store_true",
                    help="只量 P1-5 重复解析（与引擎无关，跑一次即可）")
    args = ap.parse_args()

    if args.only_dup:
        bench_dup_parse()
        return 0

    ran = 0
    for key, label, finder, rel in CASES:
        if args.engine and args.engine != key:
            continue
        game = finder()
        print()
        print("=" * 76)
        if game is None:
            # 样本不在本机：如实记为「无法验证」，不假装跑过
            print(f"{label}: 样本不在本机，无法验证（跳过）")
            print("=" * 76)
            continue
        mpath = game / rel
        print(f"{label}   {mpath}")
        print(f"存在={mpath.is_file()}")
        print("=" * 76)
        bench_open(label, mpath)
        bench_query(label, mpath)
        ran += 1

    bench_dup_parse()

    if ran == 0:
        print("\n没有可用的样本，未产出任何数字。")
        return 2
    print()
    print("=" * 76)
    print("以上为实测值。本脚本没有「通过/失败」，只有数字 ——")
    print("性能改动的验收方式是「改前改后各跑一次并对比」。")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
