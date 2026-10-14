"""响应体积上限（P2-4）：任何一次工具返回都不许把 Agent 的上下文炸掉。

## 为什么需要「统一上限」，而不是每个工具各自的 limit

契约第 5 条要求「不返回超大结果」，此前它靠**每个工具各自的** `limit` /
`has_more` 自觉遵守。实测（`devtools/verify_response_size.py`）表明这条防线
不成立 —— `limit` 的上限是 2000，Agent 完全可以合法地要 2000 条：

    search_classes(pattern="", limit=2000)      856 KB
    search_fields('a', limit=2000)              528 KB
    search_methods('a', limit=2000)             505 KB
    find_derived_classes(MonoBehaviour, 2000)   311 KB

而这些响应里 **92.8%~93.9% 的字节集中在同一个顶层列表**上（`items` / `down` /
`methods`）。所以这里把「不许超大」从**自觉**变成**机制**。

## 做法

超过 `CAP_BYTES` 时，把最大的那个顶层列表截到刚好放得下，完整列表登记成句柄，
响应里如实写明「只回了前 N 条、剩下的用哪个句柄怎么取」。

三个刻意的取舍：

1. **只截顶层列表，不做递归截断。** 实测超限字节全在顶层的那一个列表上；
   递归截断虽然更"彻底"，但会让「到底哪里被截了」变得不明显。
2. **已经有 `handle` 就不新造句柄。** `search_*` 的返回里本来就带一个指向
   **完整**结果集的句柄（`_paged` 给的），再给一个只会让 Agent 犹豫用哪个。
   此时续读的 `offset` 要**接着原来的 offset** 算，不能从 0 算（见 `_resume_at`）。
3. **测量宁可偏大，不许偏小。** 见 `measure()`：偏大只是少回几条，
   偏小就是真的超限了。
"""

from __future__ import annotations

import json
from typing import Any

#: 单次响应的线上文本上限。选 256 KB 的依据：实测最大 856 KB，
#: 取约 1/3 既能把最坏情况压下来，又不会让「要 200 条」这种正常查询被截。
#: 截断不丢数据 —— 完整列表照旧进句柄，用 `read_query_result` 取。
CAP_BYTES = 256 * 1024

#: 给「截断之后才加的那些字段」留的余量（`capped` / `cap_bytes` / `note`）。
#: 为什么必须留：`_fit` 量的试探载荷里**还没有**这几个字段，而 `note` 是中文
#: 说明（按 `ensure_ascii=True` 算，一个汉字 6 字节）。实测不留余量时，
#: `search_classes('')` 截完是 262,304 B —— **超了 160 B**。
#: 2 KB 远超任何一条说明的长度，代价只是少回几条。
RESERVE_BYTES = 2048


def measure(payload: Any) -> int:
    """估算 `payload` 在 MCP 线上文本里的字节数（**保守偏高**）。

    为什么不用 FastMCP 真正用的 `pydantic_core.to_json`：这里只要一个可靠的
    **上界**，用标准库就够，也不必依赖 `mcp` 的内部实现。
    `ensure_ascii=True` 让非 ASCII 走 `\\uXXXX`（比原样 UTF-8 更长），
    于是估出来只会偏大：偏大只是偶尔少回几条，偏小才是真的超限。

    偏大多少、以及线上真实值，由 `devtools/verify_response_size.py` 用
    「同一个 payload 分别过 `measure` 与真实 `FastMCP.call_tool`」实测钉住。
    """
    return len(
        json.dumps(payload, ensure_ascii=True, default=str,
                   indent=2, separators=(",", ": ")).encode("utf-8")
    )


def _resume_at(payload: dict, key: str, keep: int, reused: bool) -> int:
    """续读时该从第几条开始。

    **复用已有句柄时不能从 0 算**：`_paged` 的 `items` 是
    `offset .. offset+limit` 这一段，截断后要继续取的是
    `offset + keep` 之后的，而不是 `keep` 之后的。
    新登记的句柄装的就是这个列表本身，所以从 `keep` 算。
    """
    if not reused:
        return keep
    base = payload.get("offset")
    return (base if isinstance(base, int) else 0) + keep


def _fit(payload: dict, key: str, items: list, cur: int) -> int:
    """求出「最多保留几条」才能放进上限。

    平均条宽必须按**在上下文里的**宽度算。曾经拿「单独序列化这个列表」当分母，
    于是把「列表在 dict 里比独立序列化多缩进一层」的字节算进了"其它部分" ——
    实测 `search_classes('')` 因此少回约 20% 的条数（514 而不是 630）。
    改成「把列表清空再量一次」：这次测量很便宜（列表已空），而且恰好等于
    「除这个列表之外的全部字节」。

    起始值只是估算，最后仍用**真实测量**收敛（最多 6 轮）——
    末条特别宽时估算会偏大，不许只凭估算下结论。

    ## 一个被实测否决的「优化」（别再试）

    曾有一版设计想在这里省掉整包 `measure(probe)`，改成
    `other + measure(items[:keep])`（理由是「只序列化列表」）。**实测它更慢**：

        items=2000   旧 5.3ms  →  新 9.2ms   （0.58x）
        items=8000   旧 4.7ms  →  新 8.8ms   （0.53x）
        收敛多轮时   旧 24.5ms →  新 39.6ms  （0.62x）  ← 最坏情况

    原因：两种写法都要序列化**同一个大列表**，差别只在「其它部分」那点字节，
    而新写法**反而多一轮**（估算通过后还要整包校验一次才敢定稿）。
    省下的那部分可以忽略，多出来的那一轮是实打实的。
    所以这里保留整包测量 —— 它同时是**正确性**保证（收敛判据就是整包大小）。
    """
    total = len(items)
    if total <= 0:
        return 0
    budget = CAP_BYTES - RESERVE_BYTES
    base = dict(payload)
    base[key] = []
    other = measure(base)
    avg = (cur - other) / total
    if avg <= 0:
        return total
    keep = int(max(0, budget - other) / avg)
    keep = max(1, min(total, keep))
    for _ in range(6):
        probe = dict(payload)
        probe[key] = items[:keep]
        got = measure(probe)
        if got <= budget:
            return keep
        keep = max(1, int(keep * budget / got * 0.97))
    return keep


def _register(out: dict, key: str, items: list, store: Any,
              owner: str | None = None) -> tuple[str | None, bool, str, str]:
    """给被截断的完整列表找一个句柄。

    返回 `(句柄, 是否复用了已有句柄, 该写入的字段名, 新的 owner)`。

    **一次返回里可能有多个超限的顶层列表**（实测：同时带 `items` 与 `derived`
    两个大列表时两个都会被截）。这时不能都往 `out["handle"]` 上写 ——
    后一个会把前一个**覆盖掉**，于是调用方按 `out["handle"]` 去续读，
    拿到的是**另一个列表**的句柄（实测修复前 `out["handle"]` 指向 `derived`，
    而 `items` 的句柄只剩在 `note` 的散文里，结构化字段里拿不到）。

    规则：
      · `items` 且已有句柄 → 复用（`search_*` 的 `_paged` 本来就给了
        指向完整结果集的句柄，再给一个只会让 Agent 犹豫用哪个）；
      · 否则登记新句柄。`out["handle"]` **还没被别的列表占用**就写它
        （保持既有契约：单个列表被截时仍然叫 `handle`），
        已被占用则写 `out[f"{key}_handle"]` —— 两个句柄都在**结构化字段**里，
        调用方不必去解析 `note` 的散文。
    """
    if key == "items" and out.get("handle"):
        return str(out["handle"]), True, "handle", owner or key
    if store is None:
        return None, False, "handle", owner
    # 延迟导入：cap 是协议层的小工具，不该在导入期就把索引层拉起来
    from .engine.index import list_source

    handle = store.put(key, list_source(items)).handle
    if owner is None:
        return handle, False, "handle", key
    if owner == key:
        return handle, False, "handle", key
    return handle, False, f"{key}_handle", owner


def enforce(payload: Any, *, tool: str, store: Any = None) -> Any:
    """把超过 `CAP_BYTES` 的响应截断成「前 N 条 + 句柄 + 说明」。

    规则：**任何**返回都不许超过上限（成功与失败一视同仁），
    所以这里不按 `ok` 分流。没超限时原样返回（连字典都不复制）。
    """
    if not isinstance(payload, dict) or not payload:
        return payload
    if measure(payload) <= CAP_BYTES:
        return payload

    candidates = []
    for k, v in payload.items():
        if isinstance(v, list) and v:
            candidates.append((measure(v), k))
    candidates.sort(reverse=True)

    #: 通用字段（`returned` / `has_more`）归属哪个列表。
    #:
    #: 为什么需要它：这些是**不带前缀**的字段，只有**一个**列表能拥有它们。
    #: 原来对每个被截的列表都写一遍，于是两个列表都被截时，后一个会把前一个
    #: 的数字覆盖掉 —— 实测同时带 `items`（截到 1 条）与 `derived`（截到 2322 条）
    #: 时，`returned` 报的是 **2322**，而 `items` 只剩 1 条。调用方读
    #: `returned` 会以为拿到了两千多条，据此判断「数据差不多齐了」。
    #: 带前缀的（`derived_returned` / `down_has_more`）不受影响，永远各归各。
    primary = "items" if isinstance(payload.get("items"), list) else (
        candidates[0][1] if candidates else "")

    out = dict(payload)
    notes: list[str] = []
    capped = False
    #: 第一个被截断的列表占住 `out["handle"]`；后续列表写各自的
    #: `out[f"{key}_handle"]`，避免互相覆盖（见 `_register` 的说明）。
    handle_owner: str | None = None
    for _, key in candidates:
        cur = measure(out)
        if cur <= CAP_BYTES:
            break
        items = out[key]
        keep = _fit(out, key, items, cur)
        if keep >= len(items):
            continue
        out[key] = items[:keep]
        capped = True
        handle, reused, field, handle_owner = _register(
            out, key, items, store, handle_owner)
        if handle:
            out[field] = handle
        resume = _resume_at(out, key, keep, reused)

        # 说 has_more=false 却还有没给的数据，就是撒谎：截断后一律为 true。
        # 通用字段只由 primary 列表更新（否则会被别的列表改掉）；
        # 带前缀的字段永远跟着自己的列表。
        generic = (key == primary)
        for hk in ("has_more", f"{key}_has_more"):
            if hk in out and (generic or hk != "has_more"):
                out[hk] = True
        for rk in ("returned", f"{key}_returned"):
            if rk in out and (generic or rk != "returned"):
                out[rk] = keep

        head = (f"{key} 共 {len(items)} 条，超过单次响应上限 "
                f"{CAP_BYTES // 1024} KB，只回了前 {keep} 条")
        # 续读提示里的句柄名**必须与结构化字段一致**：两个列表都被截时
        # 前一个列表的句柄在 `items_handle`（不是 `handle`），
        # 提示里写错名字会让 Agent 拿着另一个列表的句柄去翻页。
        if handle and reused:
            notes.append(f"{head}；用 read_query_result(handle={handle!r}, "
                         f"offset={resume}, limit=...) 继续取"
                         f"（该句柄里有全部 {out.get('total', len(items))} 条）")
        elif handle:
            notes.append(f"{head}；完整列表已存为句柄 `{field}`={handle!r}，用 "
                         f"read_query_result({handle!r}, offset={resume}, "
                         f"limit=...) 取剩下的")
        else:
            notes.append(f"{head}；当前没有可用的句柄表，剩下的取不到 —— "
                         f"请缩小 limit 或用 pattern 过滤")

    if capped:
        out["capped"] = True
        out["cap_bytes"] = CAP_BYTES

    if measure(out) > CAP_BYTES:
        # 只可能是「体积不在列表里」（某个 dict / 超长字符串），
        # 那种情况不截，但必须如实说：宁可超限，也不许悄悄丢字段
        out["oversize"] = True
        out["cap_bytes"] = CAP_BYTES
        notes.append(f"截断顶层列表后仍超过 {CAP_BYTES // 1024} KB，"
                     f"超出部分不在列表里（工具 {tool}），请缩小查询范围")

    if notes:
        old = out.get("note")
        out["note"] = f"{old}；{'；'.join(notes)}" if old else "；".join(notes)
    return out
