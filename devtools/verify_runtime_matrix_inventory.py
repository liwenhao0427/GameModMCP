"""验收：`report_runtime_matrix.py` 的工具清单对账**必须阻断**（§11.1）。

要证明的问题
============================================================
该脚本声称「对每个游戏把 N 个工具都过一遍」，而它的 `ALL_TOOLS` 是
**手工维护**的。历史上它漏了 7 个工具（`find_instances` / `set_field_value` /
`capture_screen` / `ocr` / `probe_log` / `wait_for` / `verify_state`），
而脚本发现不一致时的处理是：

    print("  ⚠️ 清单不一致：……")     # 只打一句警告
    # 然后照常跑矩阵、照常生成报告

于是 `docs/多游戏运行时兼容性报告.md` 写着「覆盖 38 个工具；工具清单与
服务器实际报出的 38 个**逐名核对一致**」，而那 7 个工具在报告里出现 **0 次**。

这与 AGENTS.md §2.3「不许假成功」是同一类问题：**有检查，但检查不拦人**。
带病的报告比没有报告更糟 —— 它看起来是证据。

本脚本钉住两件事
============================================================
1. **清单本身是对的**：`ALL_TOOLS` 必须与服务器实际报出的工具双向完全一致
   （这里用**静态导入**拿清单，不需要起服务器；服务器侧的名字由
   `verify_tool_inventory.py` 独立核对）。
2. **不一致时真的返回 False**（`_check_tool_inventory`）—— 这是**非空转**
   的关键：只断言「现在一致」的话，把阻断逻辑删掉、退回「只打警告」
   也照样全绿。所以这里**故意构造不一致的输入**，断言它被判为失败。
3. `ALL_TOOLS` / `RUNTIME_TOOLS` / `KEY_TOOLS` 三张表之间关系自洽：
   RUNTIME ⊂ ALL、KEY ⊂ ALL、三段合起来覆盖 ALL（否则报告里会出现
   「⬜ 没跑到」的空格）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 直接导入被测脚本（它是 devtools 下的模块，不是包成员）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report_runtime_matrix as R                          # noqa: E402

import asyncio                                             # noqa: E402
from gamemod_mcp import server as S                        # noqa: E402

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
    print("=" * 76)
    print(title)
    print("=" * 76)


# ================================================================
section("1. `ALL_TOOLS` 与服务器实际报出的工具**双向完全一致**")

live = [t.name for t in asyncio.run(S.mcp.list_tools())]
print(f"  服务器实际报出 {len(live)} 个工具；ALL_TOOLS {len(R.ALL_TOOLS)} 个")

ok, detail = R._check_tool_inventory(live)
check("对账通过（ok=True）", ok is True,
      f"漏={detail['tools_missing']} 多={detail['tools_extra']}")
check("没有漏掉的工具（漏 = 报告会声称覆盖、实际没跑）",
      not detail["tools_missing"], f"{detail['tools_missing']}")
check("没有多写的工具（多 = 报告会为不存在的工具留格子）",
      not detail["tools_extra"], f"{detail['tools_extra']}")

# 点名那 7 个「历史漏掉」的工具 —— 防止有人又把它删回去
HISTORICALLY_MISSED = [
    "find_instances", "set_field_value", "capture_screen", "ocr",
    "probe_log", "wait_for", "verify_state",
]
missing_again = [t for t in HISTORICALLY_MISSED if t not in R.ALL_TOOLS]
check("7 个历史上漏掉的工具都在 ALL_TOOLS 里", not missing_again,
      f"又漏了：{missing_again}" if missing_again else
      f"{HISTORICALLY_MISSED}")

# ================================================================
section("2. **非空转**：不一致时必须判失败（不能只打警告）")

# 术语（别搞反）：`names` 是**服务器实际报出**的工具，`ALL_TOOLS` 是
# **脚本维护**的清单。
#   tools_missing = 服务器有、脚本没写  → 脚本漏了它（报告覆盖不到）
#   tools_extra   = 脚本写了、服务器没有 → 脚本多写了它（报告留空格）
#
# 反证 A：脚本漏了一个工具 —— 即**服务器报出了一个 ALL_TOOLS 里没有的名字**。
# 用真实存在的工具名（`find_instances`）配合「从 ALL_TOOLS 里删掉它」来构造，
# 这样测的就是真实会发生的形态（服务器新增了工具、脚本没同步）。
_saved = list(R.ALL_TOOLS)
try:
    R.ALL_TOOLS.remove("find_instances")      # 脚本「还没补上」这个工具
    ok_a, det_a = R._check_tool_inventory(live)
finally:
    R.ALL_TOOLS[:] = _saved
check("脚本漏了一个工具 → ok=False", ok_a is False,
      f"ok={ok_a} missing={det_a['tools_missing']}")
check("并如实报出**漏的是谁**（在 tools_missing 里）",
      det_a["tools_missing"] == ["find_instances"],
      f"missing={det_a['tools_missing']} extra={det_a['tools_extra']}")

# 反证 B：脚本多写了一个工具 —— 即 ALL_TOOLS 里有个服务器没有的名字。
try:
    R.ALL_TOOLS.append("这个工具不存在_xyz")
    ok_b, det_b = R._check_tool_inventory(live)
finally:
    R.ALL_TOOLS[:] = _saved
check("脚本多写一个不存在的工具 → ok=False", ok_b is False,
      f"ok={ok_b} extra={det_b['tools_extra']}")
check("并如实报出**多的是谁**（在 tools_extra 里）",
      det_b["tools_extra"] == ["这个工具不存在_xyz"],
      f"extra={det_b['tools_extra']} missing={det_b['tools_missing']}")

check("反证后 ALL_TOOLS 已完整还原（不污染后续断言）",
      R.ALL_TOOLS == _saved, f"{len(R.ALL_TOOLS)} vs {len(_saved)}")

# 反证 C：服务器一个都没报出来（起不来）→ 必须 False，不能「空集对空集」通过
ok_c, det_c = R._check_tool_inventory([])
check("服务器报 0 个工具 → ok=False（空清单不等于一致）", ok_c is False,
      "否则『探针起不来』会被当成『清单一致』")

# 这三条一起证明：`_check_tool_inventory` 真的会拦，不是恒 True
check("反证有效：三种不一致输入都被拦下（说明第 1 节不是恒真）",
      (ok_a, ok_b, ok_c) == (False, False, False)
      and ok is True,
      f"一致→{ok}，漏→{ok_a}，多→{ok_b}，空→{ok_c}")
# ================================================================
section("3. 三张表的关系自洽（否则报告里会有空格 / 越界）")

_all, _rt, _key = set(R.ALL_TOOLS), set(R.RUNTIME_TOOLS), set(R.KEY_TOOLS)
check("RUNTIME_TOOLS ⊆ ALL_TOOLS", _rt <= _all,
      f"越界：{sorted(_rt - _all)}" if _rt - _all else
      f"{len(_rt)} 个全在清单内")
check("KEY_TOOLS ⊆ ALL_TOOLS", _key <= _all,
      f"越界：{sorted(_key - _all)}" if _key - _all else
      f"{len(_key)} 个全在清单内")
check("KEY_STATIC 与 KEY_RUNTIME 不重叠",
      not (set(R.KEY_STATIC) & set(R.KEY_RUNTIME)),
      f"重叠：{sorted(set(R.KEY_STATIC) & set(R.KEY_RUNTIME))}")
check("UE_ONLY ⊆ ALL_TOOLS", set(R.UE_ONLY) <= _all,
      f"越界：{sorted(set(R.UE_ONLY) - _all)}")

# 报告里每张「关键工具」表都应覆盖到**全部**工具，否则那些工具在报告里
# 永远显示为 ⬜（没跑到）—— 而那正是本轮要消灭的现象。
_uncovered = sorted(_all - _key)
check("KEY_STATIC + KEY_RUNTIME 覆盖了 ALL_TOOLS 的每一个",
      not _uncovered,
      f"没进任何关键表的：{_uncovered}" if _uncovered else
      f"{len(_all)} 个工具都在表里")

# 每个「需要探针」的工具都必须在 RUNTIME_TOOLS 里 —— 否则 UE 游戏上
# 它会被记成「失败」而不是「不适用」（UE 没有托管运行时，它必然失败）。
_MUST_BE_RUNTIME = {
    "find_instances": "要有活着的探针才答得出来（枚举活实例）",
    "set_field_value": "要有活着的探针才答得出来（写游戏内存）",
    "read_field_value": "同上",
    "runtime_class_info": "同上",
    "drain_events": "同上",
}
_wrong = [f"{t}（{why}）" for t, why in _MUST_BE_RUNTIME.items()
          if t not in _rt]
check("需要探针的工具都在 RUNTIME_TOOLS 里（UE 上才会记「不适用」）",
      not _wrong, f"不在 RUNTIME_TOOLS：{_wrong}" if _wrong else
      f"{len(_MUST_BE_RUNTIME)} 个都在")

# ================================================================
section("4. 执行阶段覆盖：清单里的工具都要有地方被真跑到")

# `_check_tool_inventory` 保证了「清单 == 服务器」，但清单对了**不代表**
# 每个工具都真的会被调用 —— 漏掉执行阶段照样会出「⬜ 没跑到」。
# 这里静态检查：每个工具名是否在脚本里以 `self.call("<tool>"` /
# `self.skip("<tool>"` 的形式出现过（那些是唯一的记录入口）。
_src = (samples.ROOT / "devtools" / "report_runtime_matrix.py").read_text(
    encoding="utf-8")
_no_stage = []
for t in R.ALL_TOOLS:
    if f'self.call("{t}"' in _src or f"self.skip(\"{t}\"" in _src:
        continue
    if t in R.UE_ONLY:
        # UE 专用工具在 stage_ue 里由循环调用，逐个点名不现实
        continue
    _no_stage.append(t)
check("清单里的工具都有执行阶段（self.call / self.skip）",
      not _no_stage, f"没有阶段的：{_no_stage}" if _no_stage else
      f"{len(R.ALL_TOOLS) - len(R.UE_ONLY)} 个非 UE 工具都有")

# 点名本轮补进来的 7 个工具确实有执行阶段（防止「补了清单没补阶段」）
_patched = ["find_instances", "set_field_value", "capture_screen", "ocr",
            "probe_log", "wait_for", "verify_state"]
_ns = [t for t in _patched
       if f'self.call("{t}"' not in _src and f'self.skip("{t}"' not in _src]
check("7 个补进来的工具都有对应的执行/跳过调用", not _ns,
      f"缺阶段：{_ns}" if _ns else "每个都有 self.call / self.skip")

# ================================================================
section("5. 阻断的位置：main() 里不一致必须 `return 1`（不是继续跑）")

# 静态检查 main() 里的那段逻辑 —— 行为级的验证需要真起服务器，
# 这里退一步钉住「有 return 1」，与第 2 节的函数级断言合起来覆盖完整。
_main_src = _src[_src.index("def main() -> int:"):]
check("main() 里清单不一致会 `return 1`",
      "拒绝继续跑矩阵" in _main_src and "return 1" in _main_src,
      "只打警告、继续跑 = 检查不拦人（这就是原来的缺陷）")
check("main() 里服务器起不来也会 `return 1`",
      "拒绝生成报告" in _main_src, "清单未经核对 = 数字不可信")
check("`--from-json` 也会核对旧 JSON 的对账记录（换排版不能洗掉不一致）",
      "tools_missing" in _main_src[_main_src.index("--from-json"):]
      if "--from-json" in _main_src else "tools_missing" in _main_src,
      "旧报告若不记录覆盖范围，重排时就该拒绝")

# ================================================================
print()
print("=" * 76)
print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"，警告 {WARN} 项" if WARN else ""))
print("=" * 76)
sys.exit(1 if FAIL else 0)
