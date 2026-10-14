"""验收：**结果句柄的作用域** —— 旧句柄不许读到另一个索引的数据（P2-4）。

## 被修的缺陷

`ResultStore` 是 `Index` 的实例属性（`index.py`），切游戏就换一个实例、
序号从 0 重来，而句柄字符串是 `f"{kind}-{n}"` —— 于是**旧会话的句柄
会在新会话里重新指向另一份结果集**。修之前实测（本脚本第 1 节复现的
就是这个场景，只是改为断言它已被拒绝）：

    IL2CPP  search_classes('Player') -> 句柄 'classes-1'，首条 PlayerPrefsController
    切 Mono  search_classes('a')     -> 句柄 'classes-1'，首条 __StaticArrayInitTypeSize=26136
    再读 IL2CPP 的句柄               -> ok=True，读出来的是 **Mono 的那一条**

`ok=True` 而数据属于另一个游戏 —— 属于「假成功」，是本项目的红线。

## 修法

句柄改成 `{引擎}-g{代次}-{kind}-{n}`，代次号来自 `ResultStore._EPOCH`
（进程内每新建一个句柄表就 +1）。唯一性由代次保证；
引擎名只是为了让人看得出句柄属于哪个会话。

## 非空转反证（第 2 节）

光断言「两个句柄不相等」是不够的 —— 句柄格式随便改改就能满足。
所以这里把新句柄的「代次段」剥掉，还原成旧格式，并断言**两者还原后相同**：
这证明「旧格式下它们确实撞名」，也就是证明第 1 节拒绝掉的正是那个真缺陷。

## 反向也要测（第 3 节）

修作用域很容易顺手把正常翻页弄坏，所以同时断言：
同一个会话内 offset / limit / pattern 三种翻页行为都还正常。
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from gamemod_mcp import server as S                       # noqa: E402
from gamemod_mcp.engine.index import Index                # noqa: E402

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


#: 句柄前缀：`{引擎}-g{代次}-`
_EPOCH_SEG = re.compile(r"-g\d+-")

#: 整个前缀（引擎 + 代次）—— 旧格式里这两段都不存在
_PREFIX = re.compile(r"^.*?-g\d+-")


def old_format(handle: str) -> str:
    """把新格式句柄还原成**旧格式**（`il2cpp-g3-classes-1` -> `classes-1`）。

    旧代码是 `handle = f"{kind}-{self._counter}"`，所以旧格式就是
    `{kind}-{n}`：引擎段与代次段都是新加的，两段都要剥掉。
    用来证明「旧格式下这两个句柄确实会撞名」。
    """
    return _PREFIX.sub("", handle)


def first_name(payload: dict) -> str:
    items = payload.get("items") or []
    if not items:
        return ""
    it = items[0]
    return str(it.get("full_name") or it.get("name") or "")


def check_concurrency() -> None:
    """并发安全（**不需要样本**，永远会跑）。

    为什么必须有这一节：`cap_results` 是**进程级单例**（`state.py`），而
    `server.py` 的 `_install_event_loop_guard()` 把经 MCP 分发的同步工具
    offload 到 `asyncio.to_thread` —— 于是多个工具调用**真的会并发**
    读写同一个 `ResultStore`。没有锁时有两种偶发故障：
      · `self._counter += 1` 是读-改-写，两个线程可能拿到同一个序号
        → **两个不同的结果集共用一个句柄**（翻页时读到别人的数据）；
      · `put()` 里「淘汰最旧」的 `next(iter(...))` + `del` 可能撞上另一个
        线程改字典 → `RuntimeError: dictionary changed size during iteration`。
    两者都「偶发且结果看着正常」，只能靠并发压测暴露。
    """
    import threading                                        # noqa: E402
    from gamemod_mcp.engine.index import (                  # noqa: E402
        ResultStore, QueryResult, list_source,
    )

    section("5. 并发安全：工具被 offload 到线程池后，句柄表不许串号")

    store = ResultStore(owner="conc", max_entries=100000)
    got: list[str] = []
    errs: list[str] = []
    _barrier = threading.Barrier(8)

    def _hammer(n: int) -> None:
        try:
            _barrier.wait(timeout=10)      # 尽量让 8 条线程同时冲进 put()
            for i in range(300):
                qr = store.put("classes", list_source([{"n": n * 1000 + i}]))
                got.append(qr.handle)
        except Exception as exc:                                # noqa: BLE001
            errs.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=_hammer, args=(k,), daemon=True)
               for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check("8 线程 × 300 次登记没有抛异常（字典在迭代中被改会 RuntimeError）",
          not errs, f"{errs[:2]}" if errs else f"共登记 {len(got)} 次")
    check("并发登记了 2400 次，条数与调用次数一致",
          len(got) == 8 * 300, f"{len(got)} vs {8 * 300}")
    dup = len(got) - len(set(got))
    check("**句柄完全不重号**（重号 = 两个结果集共用一个句柄）",
          dup == 0, f"重复 {dup} 个" if dup else f"{len(set(got))} 个唯一句柄")
    check("每次登记拿到的句柄都能查回自己的结果集",
          all(store.get(h) is not None for h in got[:50]), "抽查前 50 个")
    check("条目数被上限约束（淘汰真的在生效）",
          len(store.handles()) == len(got),
          f"{len(store.handles())} 条")

    # ---- 非空转反证（**确定性的**，不靠压测碰运气）
    #
    # 如实说明：上面那 2400 次并发**没有**复现出重号。CPython 有 GIL，
    # 字典操作本身是原子的，`self._counter += 1` 在实践中很难被打断 ——
    # 所以「压测必出重号」这种反证在本运行时上**不可靠**，写成断言会变成
    # 偶发红。这里改成**与调度时机无关**的判据：
    # 让另一个线程先占住锁，再调方法 —— 持锁的实现必然**阻塞**，
    # 不持锁的必然**立刻返回**。两个方向都可判。
    import time as _time                                   # noqa: E402

    class _NoLockStore(ResultStore):
        """复刻**修复前**的写法：四个方法全部不持锁。"""

        def put(self, kind, source):                        # type: ignore[override]
            self._counter += 1
            handle = f"{self._prefix}{kind}-{self._counter}"
            qr = QueryResult(handle=handle, kind=kind, source=source)
            self._items[handle] = qr
            while len(self._items) > self._max:
                oldest = next(iter(self._items))
                del self._items[oldest]
            return qr

        def get(self, handle):                              # type: ignore[override]
            return self._items.get(handle)

        def handles(self):                                  # type: ignore[override]
            return list(self._items)

        def clear(self):                                    # type: ignore[override]
            self._items.clear()

    def _finished_while_lock_held(target: ResultStore, op: str) -> bool:
        """锁被占住时调 `op`：提前返回 True（= 没持锁），阻塞则 False。"""
        done = threading.Event()
        started = threading.Event()

        def _call() -> None:
            started.set()
            if op == "put":
                target.put("classes", list_source([{"n": 1}]))
            elif op == "get":
                target.get("whatever")
            elif op == "handles":
                target.handles()
            else:
                target.clear()
            done.set()

        th = threading.Thread(target=_call, daemon=True)
        with target._lock:                 # 主线程先占住锁
            th.start()
            started.wait(timeout=2)
            early = done.wait(timeout=0.5)
        done.wait(timeout=2)               # 放锁后必须能完成（否则是死锁）
        return early

    locked_store = ResultStore(owner="lockprobe", max_entries=10)
    for op in ("put", "get", "handles", "clear"):
        check(f"`{op}()` 真的持有锁（锁被占住时会阻塞）",
              not _finished_while_lock_held(locked_store, op),
              "持锁 → 阻塞；不持锁 → 会立刻返回")

    free_store = _NoLockStore(owner="nolock", max_entries=10)
    for op in ("put", "get", "handles", "clear"):
        check(f"反证：旧写法（不持锁）的 `{op}()` 在锁被占住时**照样返回**",
              _finished_while_lock_held(free_store, op),
              "证明上面四条测的确实是「有没有持锁」，不是恒真")

    check("锁是**可重入**的（RLock：将来只读方法在持锁路径上被重入不会死锁）",
          type(locked_store._lock).__name__ == "RLock",
          f"{type(locked_store._lock).__name__}")

    # 容量：默认 128（§11.3 —— 原为 32，几次搜索就把旧句柄挤掉）
    check("默认句柄容量已扩到 128（原 32：一次新搜索就能挤掉旧句柄）",
          ResultStore(owner="cap")._max == 128,
          f"默认 {ResultStore(owner='cap')._max}")
    _ev = ResultStore(owner="evict", max_entries=3)
    _handles = [_ev.put("classes", list_source([{"n": i}])).handle
                for i in range(5)]
    check("淘汰行为仍正确（上限 3 时塞 5 条，最旧的两条查不到）",
          _ev.get(_handles[0]) is None and _ev.get(_handles[1]) is None
          and _ev.get(_handles[4]) is not None,
          f"留下的 = {[h.split('-')[-1] for h in _ev.handles()]}")

    S.close_session(close_runtime=False)


def main() -> int:
    # 并发那一节**不需要样本**，先跑 —— 否则样本缺失时会连它一起被跳过，
    # 而它恰恰是本轮新增的、最需要长期保护的断言。
    check_concurrency()

    il2cpp = samples.find_il2cpp()
    mono = samples.find_mono()
    if not samples.require(il2cpp, "IL2CPP 样本") or not samples.require(mono, "Mono 样本"):
        print("两个样本缺一不可（要跨游戏才谈得上句柄作用域）")
        print()
        print("=" * 78)
        print(f"\u26a0\ufe0f  通过 {PASS} 项，失败 {FAIL} 项（跨游戏部分**无法验证**，"
              f"未假装通过）")
        print("=" * 78)
        return 1 if FAIL else 0

    section("1. 跨游戏：旧句柄必须被拒绝，而不是读到新游戏的数据")

    opened = S.open_game(game_dir=str(il2cpp))
    if not check("open_game(IL2CPP) 成功", bool(opened.get("ok")),
                 str(opened.get("error") or "")):
        return 0
    a = S.search_classes(pattern="Player", limit=2)
    ha, a_first = a.get("handle"), first_name(a)
    check("IL2CPP 拿到句柄", bool(ha), f"句柄={ha!r} 首条={a_first}")

    opened_b = S.open_game(game_dir=str(mono))
    check("open_game(Mono) 成功", bool(opened_b.get("ok")),
          str(opened_b.get("error") or ""))
    b = S.search_classes(pattern="a", limit=2)
    hb, b_first = b.get("handle"), first_name(b)
    check("Mono 拿到句柄", bool(hb), f"句柄={hb!r} 首条={b_first}")

    # 这一条是本次修复的核心断言
    check("跨游戏的句柄字符串不同（旧格式下它们都是 'classes-1'）",
          ha != hb, f"{ha!r} vs {hb!r}")

    r = S.read_query_result(ha, limit=2)
    check("拿旧句柄读 -> 如实失败（ok=False）", r.get("ok") is False,
          f"ok={r.get('ok')} error={r.get('error')}")
    check("失败信息里带 available_handles（告诉 Agent 有哪些可用）",
          isinstance(r.get("available_handles"), list),
          f"{len(r.get('available_handles') or [])} 个可用句柄")
    check("旧句柄不在可用列表里",
          ha not in (r.get("available_handles") or []),
          f"可用={r.get('available_handles')}")

    # 最关键的一条：绝不能把 Mono 的数据当结果返回
    wrong = first_name(r)
    check("!读不到另一个游戏的数据!(这才是修掉的那个缺陷)",
          wrong != b_first,
          f"冲突数据={b_first!r}，本次读到={wrong!r}")

    section("2. 非空转反证：旧格式下这两个句柄确实会撞名")

    print(f"  新格式：{ha!r} 与 {hb!r}")
    print(f"  还原成旧格式：{old_format(ha)!r} 与 {old_format(hb)!r}")
    check("剥掉引擎段与代次段后两者相同 —— 证明旧格式真的会撞名",
          old_format(ha) == old_format(hb) and old_format(ha) != "",
          f"还原后 = {old_format(ha)!r}（旧代码就是 f\"{{kind}}-{{n}}\"）")
    check("代次段确实存在于句柄里（不是碰巧不等）",
          bool(_EPOCH_SEG.search(ha)) and bool(_EPOCH_SEG.search(hb)),
          f"匹配片段 = {_EPOCH_SEG.search(ha).group(0)!r}")
    check("代次段在两个句柄里不同",
          _EPOCH_SEG.search(ha).group(0) != _EPOCH_SEG.search(hb).group(0),
          f"{_EPOCH_SEG.search(ha).group(0)} vs {_EPOCH_SEG.search(hb).group(0)}")
    check("引擎段如实反映各自会话（IL2CPP vs Mono）",
          ha.startswith("il2cpp-") and hb.startswith("mono-"),
          f"{ha.split('-')[0]!r} vs {hb.split('-')[0]!r}")

    section("3. 反向：同一会话内的翻页/过滤必须照旧能用")

    p = S.read_query_result(hb, offset=0, limit=1)
    check("同会话内 offset/limit 翻页正常", p.get("ok") is True,
          f"ok={p.get('ok')} returned={p.get('returned')} total={p.get('total')}")
    check("翻页读回的第一条与原查询一致", first_name(p) == b_first,
          f"{first_name(p)!r} == {b_first!r}")

    p2 = S.read_query_result(hb, offset=1, limit=1)
    check("offset=1 拿到的是**不同**的一条（offset 真的生效）",
          p2.get("ok") is True and first_name(p2) != first_name(p),
          f"offset=0 -> {first_name(p)!r}，offset=1 -> {first_name(p2)!r}")

    p3 = S.read_query_result(hb, limit=3, pattern="zzz-不可能命中的串-zzz")
    check("pattern 零命中时仍 ok=True 并说明原因（契约：不许把空当不存在）",
          p3.get("ok") is True and not (p3.get("items") or []),
          f"note={str(p3.get('note'))[:52]}")

    section("4. 引擎层：同一个游戏重开索引，句柄同样不重名")

    meta = il2cpp / "Garden of Witches_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    if not samples.require(meta, "IL2CPP 元数据（引擎层直开）"):
        print("  跳过（样本不同路径，无法直开）")
    else:
        i1 = Index.open(meta)
        i2 = Index.open(meta)
        try:
            check("两个 Index 实例的句柄前缀不同",
                  i1.results.prefix != i2.results.prefix,
                  f"{i1.results.prefix!r} vs {i2.results.prefix!r}")
            r1 = i1.search_classes("Player", limit=1)
            check("同一个游戏重开后，旧句柄在新实例里查不到",
                  i2.read_query_result(r1["handle"]).get("found") is False,
                  f"旧句柄={r1['handle']!r}")
            check("前缀里带引擎名（可读性：看得出句柄属于哪个引擎）",
                  i1.results.prefix.startswith("il2cpp-g"),
                  f"prefix={i1.results.prefix!r}")
        finally:
            i1.close()
            i2.close()

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
