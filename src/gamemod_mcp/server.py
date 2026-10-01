"""GameMod MCP 服务器：把游戏 Mod 开发能力暴露成标准 MCP 工具。

设计说明
============================================================
本文件是「通用 Agent 接入点」。任何支持 MCP 的客户端
（Claude Code / Codex / Cursor / 自研 Agent）都能通过 stdio
连上这里，获得一整套游戏 Mod 开发工具。

与 AiMod 的关系
------------------------------------------------------------
AiMod 是把「云 LLM + 本地 MCP 工具（26 个）」打包成一个 Tauri 应用，
并且带订阅校验、租约、工具收据签名等商业机制。
本项目取它的**技术思路**（元数据优先定位、运行时探针、增量事件），
但不含任何 AiMod 代码，也不依赖它的云服务：
    · 无云依赖 —— 全部本地计算
    · 无授权校验 —— 装上就能用
    · 工具面按「通用 Agent 的实际需要」重新设计，而不是照抄它的 26 个

工具分层
------------------------------------------------------------
    1. 发现层      : find_game_processes / inspect_game / inspect_binary
    2. 会话层      : open_game / get_session / close_session
    3. 静态查询层  : search_classes / class_detail / search_fields /
                     search_methods / class_hierarchy / find_derived_classes /
                     analyze_dependency / list_images / decompile
    4. 结果句柄层  : read_query_result
    5. 运行时层    : inject_process / launch_game / runtime_info /
                     runtime_class_info / find_hook_target / hook_method /
                     hook_address / unhook / hook_status / drain_events /
                     read_memory / write_memory / read_field_value /
                     scan_pattern / eval_script / gc_control
    6. UE 层       : detect_ue / ue_profile / ue_list_entries /
                     ue_script_objects / ue_manifest
    7. 诊断层      : get_diagnostics

关键约定
------------------------------------------------------------
    · 所有工具返回 dict，且**必定**带 `ok` 字段（成功 True / 失败 False）。
    · 失败时带 `error` 字段说明原因，绝不返回看似成功的空数据。
    · 日志一律走 stderr（见 logutil），因为 stdout 是 JSON-RPC 通道。
    · **单次返回不超过 256 KB**，由 `cap.py` 在外层统一兜底（见下）。
      超限时截断最大的顶层列表、完整列表进句柄，返回里会有 `capped: true`
      与 `note`（写明用 `read_query_result` 怎么取回剩下的）。

响应体积上限（P2-4）
------------------------------------------------------------
    每个工具都被 `@_cap_response` 包了一层。为什么不在返回里各写各的：
    38 个工具里只有 25 个走 `_ok()`，而超限最严重的
    find_derived_classes / class_hierarchy / class_detail / runtime_class_info
    恰恰都不走它 —— 想要「一处拦住」就只能包在工具函数外层。
    已有的 `handle` 会被复用（不另造句柄）；`has_more` / `returned`
    一律按**实际**返回改写：明明还有却说没有，比超限更糟。

资源（MCP resources，P2-2）
------------------------------------------------------------
    只有一个：`gamemod://guide`，内容是本 MCP 自己的使用指南
    （与 `skills/gamemod-mod/SKILL.md` 同源）—— 这是全项目唯一
    **没有任何工具出口**的内容，所以放进资源不算重复。
    `get_session` / `open_game` 已经返回引擎能力矩阵，故**不做**
    `gamemod://capabilities`（那会是同一份数据的第二个出口）。
    **不做 prompts**：实测客户端（DSH）的 MCP 客户端实现了
    `resources/list|read`，却完全没有 prompts 相关方法，也没有
    `dsh-mcp-prompts` 这类包 —— 加了也传不到模型面前。
    资源不走 `@_cap_response`（它是工具层的外壳）：内容是受控的静态文件，
    体积远小于上限，验收脚本里卡了这条。

静态与运行时的能力边界（**不许说反**）
------------------------------------------------------------
    静态元数据里有: 类 / 字段 / 方法的**存在与签名**、方法 token
    静态元数据里没有: 字段字节偏移、方法 RVA/绝对地址
    → 偏移与地址**只能**靠注入后的 runtime_class_info / find_hook_target 取。
      class_detail 给出的 token 是连接两侧的钥匙：静态 token 与运行时
      token 相等，才是「Hook 落点正确」的证据。
"""

from __future__ import annotations

import functools
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# 两种启动方式都必须能用（文档与客户端配置里两种都出现过）：
#   1. `python -m gamemod_mcp.server`（配 PYTHONPATH=<repo>/src）
#   2. `python <repo>/src/gamemod_mcp/server.py`
# 第 2 种方式下没有父包，`__package__` 是空串，相对导入会当场炸：
#   ImportError: attempted relative import with no known parent package
# 而这个报错**发生在任何工具注册之前**，客户端只会看到
# `McpError: Connection closed` —— 完全看不出是入口写法的问题。
# 所以这里先把包所在目录（src/）塞进 sys.path，再按包名做绝对导入。
if __package__ in (None, ""):  # 直接当脚本跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from gamemod_mcp import __version__, cap
    from gamemod_mcp.engine import decompile as dc
    from gamemod_mcp.engine import discovery, state
    from gamemod_mcp.engine import ue as ue_mod
    from gamemod_mcp.engine.state import ensure_index, ensure_runtime, select_game
    from gamemod_mcp.logutil import progress, warn
else:  # 正常作为包被导入
    from . import __version__, cap
    from .engine import decompile as dc
    from .engine import discovery, state
    from .engine import ue as ue_mod
    from .engine.state import ensure_index, ensure_runtime, select_game
    from .logutil import progress, warn

# 注意：上面这些导入原本有一部分写在**函数体里**（`from .engine import ue`、
# `from .logutil import warn`）。惰性导入本身没问题，但用相对形式就有问题：
# 以脚本方式启动时没有父包，`from .x import y` 会在**调用那个工具时**才炸
# （initialize 与 tools/list 都能过，看起来一切正常，一用就 ImportError）。
# 入口要同时支持两种形式，所以这些导入都收敛到这里 ——
# 两个模块只依赖标准库与同包模块，提前导入没有额外代价。

# ---------------------------------------------------------------- 服务器实例

mcp = FastMCP(
    "gamemod",
    instructions=(
        "GameMod MCP —— 游戏 Mod 开发的通用工具集。\n"
        "\n"
        "典型工作流（强烈建议按顺序走）：\n"
        "  1. open_game(game_dir=...)  选定游戏并准备好元数据索引\n"
        "  2. search_classes(...)      搜索类，拿到 full_name\n"
        "  3. class_detail(...)        细看字段/方法/token/inheritance；\n"
        "                              给 method_name 可只看某个方法的重载\n"
        "  4. inject_process / launch_game  注入运行中的游戏，或启动并注入\n"
        "  5. runtime_class_info(...)  取字段的**真实运行时偏移**与方法真实地址\n"
        "  6. hook_method(...)         在目标方法上装 Hook\n"
        "  7. drain_events()           读取 Hook 捕获到的调用事件\n"
        "\n"
        "核心思路：**先用静态元数据定位，再用运行时确认**。\n"
        "但要注意两侧的能力边界（很容易记反）：\n"
        "  · 静态元数据里有类/字段/方法的存在与签名、以及方法 token；\n"
        "  · 静态元数据里**没有**字段字节偏移，**也没有**方法 RVA/绝对地址；\n"
        "  · 偏移与地址**只能**靠注入后的运行时工具取。\n"
        "  · class_detail 给的 token 是连接两侧的钥匙：静态 token == 运行时 token，\n"
        "    才是「Hook 落点正确」的证据。\n"
        "\n"
        "注意：第 1~3 步是纯静态查询，**不需要启动游戏**；\n"
        "第 4~7 步需要游戏可注入，且运行 agent 的进程要有足够权限\n"
        "（Windows 上通常需要管理员）。\n"
        "\n"
        "响应体积上限：任何一次返回不超过 256 KB。被截断时返回里会有\n"
        "`capped: true` 与 `note`，照 note 给的\n"
        "read_query_result(handle=..., offset=...) 就能取回剩下的（一条不丢）；\n"
        "此时 `has_more` / `returned` 已按**实际**返回改写，不是原来的请求值。\n"
        "\n"
        "需要完整的使用指南（配方、引擎差异、常见坑）时，读资源\n"
        "gamemod://guide —— 它与 skills/gamemod-mod/SKILL.md 同源。"
    ),
)


# ---------------------------------------------------------------- 小工具


def _ok(**kw: Any) -> dict:
    return {"ok": True, **kw}


def _err(msg: str, **kw: Any) -> dict:
    return {"ok": False, "error": msg, **kw}


# ------------------------------------------------------- 响应体积上限（P2-4）
#
# 契约第 5 条「不返回超大结果」以前靠每个工具各自的 `limit` 自觉遵守，
# 但 `limit` 上限是 2000，Agent 完全可以合法地要 2000 条 —— 实测那样一条
# `search_classes('')` 的线上文本是 **856 KB**。现在由 cap.py 统一兜底：
# 超限就截断最大的那个顶层列表，完整列表进句柄，并如实说明剩下的怎么取。
#
# 为什么用装饰器而不是在 `_ok` 里拦：**38 个工具里只有 25 个走 `_ok`**，
# 而超限最严重的 `find_derived_classes` / `class_hierarchy` /
# `class_detail` / `runtime_class_info` 恰恰都不走它。装饰器挂在工具函数上，
# 拿到的是「最终要发出去的 dict」，不会漏。
#
# 装饰器顺序：`@mcp.tool(...)` 在上、`@_cap_response` 在下 —— 于是
# 先套上限再注册，注册进去的就是套好的那个函数。
# `functools.wraps` 保住 `__wrapped__`，`inspect.signature` 会跟着它走，
# 所以**入参 schema 一个字节都不会变**（由 verify_cap.py 逐字比对钉住）。


#: 未套响应上限的**原函数**：`{工具名: 函数}`，在装饰时登记。
#:
#: 用途有两个，都是「必须拿到截断前的真实返回」：
#:   · `verify_response_size.py` 量「截断前有多大」做前后对比；
#:   · `verify_cap.py` 比对「装饰器有没有改变入参签名」。
#:
#: 为什么不直接用 `S.<工具>.__wrapped__`（明明 `functools.wraps` 就给了）：
#: 动态覆盖率垫片（`devtools/_cov/sitecustomize.py`）会在导入完成后把
#: `server` 模块上的工具属性换成它自己的**记录包装器**，那个包装器没有
#: `__wrapped__`。于是脚本会「单独跑全绿、进套件就红」——
#: 本轮真的踩到了（`verify_cap` / `verify_response_size` 都是这样挂的）。
#: 显式登记原函数就不受任何外层替换影响。
RAW_TOOLS: dict[str, Any] = {}


def _cap_response(fn: Any) -> Any:
    """给工具函数套上响应体积上限。"""
    RAW_TOOLS[fn.__name__] = fn

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        payload = fn(*args, **kwargs)
        try:
            return cap.enforce(payload, tool=fn.__name__,
                               store=state.state().cap_results)
        except Exception as exc:      # noqa: BLE001
            # 上限检查是**附加**保护，绝不能因为它自己出问题就让一个
            # 本来正常的工具失败（那会把「省上下文」变成「功能坏了」）。
            # 但也不许静默：打到 stderr，让人看得见。
            warn(f"响应体积上限检查失败，已按原样返回：{type(exc).__name__}: {exc}")
            return payload

    return wrapper


# ------------------------------------------------------- MCP 工具语义标注
#
# MCP 的 `annotations` 有 4 个语义位，**默认值不是「都关」**：
#   readOnlyHint 默认 False、destructiveHint 默认 **True**、
#   idempotentHint 默认 False、openWorldHint 默认 **True**。
# 也就是说：不写 `destructiveHint`，客户端只能按「它会破坏东西」处理；
# 不写 `openWorldHint`，只能按「它与外部世界交互」处理。默认值是**保守**的，
# 但用在本项目上会把 `search_classes`（只读本地 SQLite）也说成危险操作。
# 所以这里**每个**工具都显式表态，不把语义留给默认值。
#
# 几个**曾经写错 / 容易写错**的点（改工具时回头看）：
#   · `drain_events` 曾标 `readOnlyHint=True`，但它是「**取走即消费**」——
#     事件被消费掉就没了，所以是 readOnly=False + destructiveHint=True。
#   · `decompile` / `ue_profile` 给了 output_dir / output_path 就会**落盘**，
#     所以不是只读（但它们只写自己的产物、可覆盖，不算破坏）。
#   · 纯查索引的工具只读本地 SQLite，既不碰游戏进程也不联网
#     → openWorldHint=False；一碰活着的游戏进程就是 True。


def _ann(title: str, *, read_only: bool, open_world: bool = False,
         destroy: bool | None = None,
         idempotent: bool | None = None) -> dict:
    """构造 MCP 工具的 `annotations`，把 4 个语义位写全。

    Args:
        title:      给人看的标题。
        read_only:  不改动任何东西（不写内存、不装 Hook、不落盘、不消费缓冲）。
        open_world: 依赖本进程之外的东西（运行中的游戏进程）。默认 False。
        destroy:    结果不可逆 / 可能让游戏崩 / 数据被消费掉。
                    **只读工具不允许为 True**（会直接抛错，避免自相矛盾的标注）；
                    非只读工具默认 True（沿用 MCP 的保守默认）。
        idempotent: 同样参数再调一次不产生额外影响。只读工具恒为 True
                    （没有任何副作用），非只读默认 False。

    为什么用函数而不是到处写 dict：只读与「不破坏 / 无副作用」是**蕴含关系**，
    写成函数就能在**导入期**就把 `read_only=True` + `destroy=True` 这种
    自相矛盾的组合打出来，而不是等客户端看到怪标注。
    """
    if read_only:
        if destroy is True:
            raise ValueError(
                f"工具「{title}」声明为只读，却又声明 destructiveHint=True —— "
                f"两者矛盾：只读工具不可能破坏任何东西")
        destroy = False
        idempotent = True if idempotent is None else idempotent
    else:
        destroy = True if destroy is None else destroy
        idempotent = False if idempotent is None else idempotent
    return {
        "title": title,
        "readOnlyHint": read_only,
        "destructiveHint": destroy,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


def _need_index() -> tuple[Any, dict | None]:
    """取当前索引；没有就返回错误响应。"""
    st = state.state()
    if not st.has_index():
        return None, _err(
            "当前会话还没有元数据索引。请先调用 open_game(game_dir=...) "
            "选定游戏，或用 open_game(metadata_path=...) 直接指定元数据文件。",
            session=st.summary(),
        )
    return st.index, None


def _index_query(res: dict | None, what: str, **extra: Any) -> dict:
    """统一收口索引层查询结果。

    索引层用 `found=False` 表示「查了但没这个对象」（而不是抛异常或返回 None）。
    如果直接把这种结果当成成功返回，Agent 会误以为查到了空数据 ——
    这是最容易骗到 Agent 的一类假成功，必须在这里拦掉。

    `extra` 是调用方给的**兜底**提示（如「用 search_classes 先确认类名」），
    只在索引层**没给**同名键时才补上。索引层的提示更具体
    （例如歧义时它会给出 `candidates` 和「用 assembly 消歧」的 hint），
    不能被调用方的泛泛提示覆盖，也不能因为键重名直接抛 TypeError ——
    曾经 `_err()` 因此 `got multiple values for keyword argument 'hint'`。
    """
    if res is None:
        return _err(f"未找到{what}", **extra)
    if not res.get("found", True):
        reason = res.get("reason") or f"未找到{what}"
        payload = {k: v for k, v in res.items() if k not in ("found", "reason")}
        for k, v in extra.items():
            payload.setdefault(k, v)     # 索引层的更具体，优先保留
        return _err(reason, **payload)
    return _ok(**res)


def _split_argv(text: str) -> list[str]:
    """按 Windows 习惯拆分命令行参数串。

    规则：空白分隔；**双引号内保留空白**，引号本身去掉；反斜杠**不是**转义符。

    为什么不用 `shlex.split`（本文件曾经的建议）：实测

        shlex.split(r'--path C:\\Games\\My Game\\a.exe --x')
        posix=True （默认）-> ['--path', 'C:GamesMy', 'Gamea.exe', '--x']   # 反斜杠被吃掉
        posix=False        -> ['--path', 'C:\\\\Games\\\\My', 'Game\\\\a.exe', '--x']  # 引号没生效

    默认的 posix 模式把 `\\` 当转义符，而 Windows 路径全是反斜杠 ——
    照那条建议改下去会把 `C:\\Games\\Game\\a.exe` 变成 `C:GamesGamea.exe`，
    是**引进**一个 bug 而不是修掉一个。`posix=False` 又会把引号留在 token 里。
    所以这里自己拆：本项目只跑 Windows，参数就是「空格分隔 + 引号括路径」。

    未闭合的引号按「一直引到串尾」处理（不报错，与多数启动器一致）；
    空的 `""` 会被丢掉（不产生空 token）。
    """
    out: list[str] = []
    cur: list[str] = []
    in_quote = False
    for ch in text:
        if ch == '"':
            in_quote = not in_quote
        elif ch.isspace() and not in_quote:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


#: 字段类型名 → 读取宽度（字节）。
#:
#: 为什么需要它：`read_field_value` 原来**固定读 8 字节**。字段只有 4 字节时，
#: 那 4 个「多读的」字节是**相邻字段**的内容 —— 既会给出无意义的
#: `as_int64`（两个字段拼出来的数），又会在字段贴近页尾时把一次**本可成功**
#: 的读取变成假失败。宽度按字段类型来，才不会读到别人的字节。
#:
#: 键同时收 IL2CPP 的 `il2cpp_type_get_name` 形式（`System.Int32`）与短名，
#: 因为运行时返回的是前者；查不到就按 8 字节（**引用类型/指针**在 x64 上
#: 就是 8 字节，且这是原来的行为，不会把没覆盖的类型改坏）。
_FIELD_WIDTH: dict[str, int] = {
    "System.Boolean": 1, "bool": 1,
    "System.Byte": 1, "byte": 1, "System.SByte": 1, "sbyte": 1,
    "System.Char": 2, "char": 2,
    "System.Int16": 2, "short": 2, "System.UInt16": 2, "ushort": 2,
    "System.Int32": 4, "int": 4, "System.UInt32": 4, "uint": 4,
    "System.Single": 4, "float": 4,
    "System.Int64": 8, "long": 8, "System.UInt64": 8, "ulong": 8,
    "System.Double": 8, "double": 8, "System.IntPtr": 8, "System.UIntPtr": 8,
}


def _field_read_width(type_name: Any) -> int:
    """字段类型名 → 要读几个字节。查不到按 8（指针宽度，也是原来的行为）。"""
    if not type_name:
        return 8
    return _FIELD_WIDTH.get(str(type_name).strip(), 8)


# ================================================================
# 1. 发现层
# ================================================================


@mcp.tool(annotations=_ann("列出运行中的游戏进程", read_only=True, open_world=True))
@_cap_response
def find_game_processes(name_filter: str = "", only_games: bool = True) -> dict:
    """列出正在运行的游戏进程，并识别其运行时（il2cpp / mono / 其它）。

    Args:
        name_filter: 进程名子串过滤（大小写不敏感）。
        only_games:  只要看起来像游戏的进程（默认 True）。

    Returns:
        {ok, count, processes: [{pid, name, exe_path, window_title,
                                 runtime, is_64}]}
        `is_64` 为 None 表示**没测出来**（拿不到进程句柄），不是「32 位」。
    """
    try:
        procs = discovery.find_processes(name_filter=name_filter,
                                         only_games=only_games)
    except Exception as exc:
        return _err(f"枚举进程失败：{exc}")

    return _ok(
        count=len(procs),
        processes=[
            {
                "pid": p.pid,
                "name": p.name,
                "exe_path": p.exe_path,
                "window_title": p.window_title,
                "runtime": p.runtime,
                "is_64": p.is_64,
            }
            for p in procs
        ],
    )


@mcp.tool(annotations=_ann("探测游戏安装目录", read_only=True))
@_cap_response
def inspect_game(game_dir: str) -> dict:
    """探测一个游戏目录：识别引擎、找出主程序与关键文件。

    本项目的使用方式是「一次只针对一个游戏项目目录」，
    所以不必去扫全机有哪些游戏 —— 直接把目录传进来即可。

    Args:
        game_dir: 游戏安装目录。

    Returns:
        {ok, engine, main_exe, il2cpp/mono/ue 的关键路径, ...}
    """
    if not game_dir:
        return _err("必须提供 game_dir")
    try:
        info = discovery.describe_game(game_dir)
    except Exception as exc:
        return _err(f"探测失败：{exc}", game_dir=game_dir)
    if not info.get("exists"):
        return _err(f"目录不存在：{game_dir}")
    return _ok(**info)


@mcp.tool(annotations=_ann("分析可执行文件结构", read_only=True))
@_cap_response
def inspect_binary(path: str) -> dict:
    """分析 PE 文件（exe/dll）：架构、节区、导出表、导入表、运行时类型。

    用于判断游戏是否加壳、是否托管、以及找出可 Hook 的导出函数。

    关键结论（architecture / runtime / export_count / section_count）
    会**同时**提到顶层与 `pe` 子对象里：Agent 通常只关心「这是不是 x64」
    「这是不是 .NET」「有没有导出函数」，不该为了拿这几个值多下钻一层。
    完整的节区表、导入 DLL 列表留在 `pe` 里。

    Args:
        path: 可执行文件或 DLL 路径。
    """
    res = state.inspect_binary(path)
    if not res.get("ok"):
        return res
    pe = res.get("pe") or {}
    # 摘掉几个最常被问到的字段提到顶层（保留 pe 里的原件，不做搬移）
    for key in ("size", "machine", "is_64", "image_base", "entry_point_rva",
                "subsystem", "runtime", "is_dotnet", "is_nativeaot",
                "section_count", "export_count", "import_dll_count"):
        if key in pe:
            res.setdefault(key, pe[key])
    return res


# ================================================================
# 2. 会话层
# ================================================================


@mcp.tool(annotations=_ann("选定游戏并准备索引", read_only=False, destroy=False, idempotent=True))
@_cap_response
def open_game(
    game_dir: str = "",
    metadata_path: str = "",
    engine: str = "",
    cache_dir: str = "",
    force_rebuild: bool = False,
) -> dict:
    """选定游戏，并构建（或复用）它的元数据索引。

    之后所有查询工具都以这个游戏为上下文。切换游戏会重置会话
    （已注入的运行时会被安全卸载）。

    支持三种引擎，会自动识别：
      · IL2CPP — 元数据是 global-metadata.dat，逻辑在 GameAssembly.dll
      · Mono   — 元数据是托管 dll（通常是 Assembly-CSharp.dll）
      · UE     — 无集中元数据（能力以 get_diagnostics 报告为准）

    重复调用同一个游戏**不会重建**（返回 reused=true，通常 10ms 内返回）。

    Args:
        game_dir:      游戏安装目录（推荐；会自动识别引擎并定位元数据文件）。
        metadata_path: 直接指定元数据文件（IL2CPP 用 global-metadata.dat，
                       Mono 用某个 .dll；目录结构不标准时用）。
        engine:        强制指定引擎（il2cpp / mono / ue），一般不用填。
        cache_dir:     索引缓存目录，默认 ~/.gamemod/cache。
        force_rebuild: 强制重建索引（怀疑缓存损坏时用）。

    Returns:
        {ok, engine, engine_label, metadata_path, index: {type_count,
         method_count, field_count, capabilities...}, elapsed, reused}
    """
    sel = select_game(game_dir=game_dir, metadata_path=metadata_path,
                      engine=engine)
    if not sel.get("ok"):
        return sel
    res = ensure_index(cache_dir=cache_dir, force_rebuild=force_rebuild)
    if not res.get("ok"):
        return res
    # 把「这个引擎能提供什么」显式带出来，Agent 就不会把
    # 「IL2CPP 元数据里没有字段偏移」误判成工具坏了。
    res["engine"] = sel.get("engine", "")
    res["engine_label"] = sel.get("engine_label", "")
    return res


@mcp.tool(annotations=_ann("查看当前会话状态", read_only=True))
@_cap_response
def get_session() -> dict:
    """查看当前会话状态：选中的游戏、索引统计、运行时注入情况。

    排查「为什么查不到类 / Hook 不上」时，先看这个。
    """
    st = state.state()
    return _ok(**st.summary())


@mcp.tool(annotations=_ann("关闭会话", read_only=False, destroy=False, idempotent=True))
@_cap_response
def close_session(close_runtime: bool = True, close_index: bool = True) -> dict:
    """关闭当前会话：卸载所有 Hook、断开注入、关闭索引。

    做完了就调用它，把游戏恢复原状（卸载 Hook 后游戏行为回到未修改状态）。

    **只有真的恢复成功才返回 `ok: True`。** 卸载 Hook / 卸载脚本 / detach
    任何一步失败都会 `ok: False` + `error`，并保留会话以便重试 ——
    这是有意为之：本工具的语义是「游戏已恢复原状」，
    一个假成功会让 Agent 带着仍被篡改的游戏进程收工。
    """
    st = state.state()
    out: dict[str, Any] = {}
    failures: list[str] = []
    if close_runtime:
        rt = st.close_runtime()
        out["runtime"] = rt
        if not rt.get("ok"):
            failures.append(str(rt.get("error") or "运行时会话关闭失败"))
    if close_index:
        ix = st.close_index()
        out["index"] = ix
        if not ix.get("ok"):
            failures.append(str(ix.get("error") or "索引关闭失败"))
    if failures:
        out["restored"] = False
        return _err("；".join(failures), **out)
    out["restored"] = True
    return _ok(**out)


# ================================================================
# 3. 静态查询层
# ================================================================


@mcp.tool(annotations=_ann("搜索类/结构/枚举", read_only=True))
@_cap_response
def search_classes(
    pattern: str = "",
    offset: int = 0,
    limit: int = 50,
    subtype_of: str = "",
    assembly: str = "",
    only_enums: bool = False,
    exclude_compiler_generated: bool = True,
) -> dict:
    """搜索游戏里的类、结构体、枚举 —— Mod 开发最主要的入口。

    返回结果里带 `handle`，结果集较大时可用 read_query_result 翻页，
    并可用它的 `pattern` 参数在结果内再过滤。

    Args:
        pattern:    名称子串（大小写不敏感）。空表示不过滤。
        subtype_of: 只要继承自该类型的（如 "UnityEngine.MonoBehaviour"）。
        assembly:   按程序集名过滤（子串，如 "TeamTapas"）。
        only_enums: 只要枚举（找游戏常量表很有用）。
        limit:      本页最多返回多少条（上限 2000）。

    Returns:
        {ok, handle, total, returned, has_more, items: [{full_name, nested_name,
         namespace, method_count, field_count, is_enum, image, token}]}
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.search_classes(
            pattern=pattern, offset=offset, limit=limit,
            subtype_of=subtype_of, assembly=assembly, only_enums=only_enums,
            exclude_compiler_generated=exclude_compiler_generated,
        )
        return _ok(**res)
    except Exception as exc:
        return _err(f"搜索失败：{exc}")


@mcp.tool(annotations=_ann("查看类的完整细节", read_only=True))
@_cap_response
def class_detail(full_name: str, include_inherited: bool = False,
                 method_name: str = "", assembly: str = "") -> dict:
    """查看一个类的完整信息：字段（含类型）、方法（含 token）、继承链。

    **这是定位 Hook 落点的核心工具**：方法的 token 由这里给出，
    再配合 runtime_class_info / find_hook_target 就能拿到真实的运行时地址。

    重要：**静态元数据里没有字段偏移、也没有方法地址**（只有 token）。
    字段偏移与方法真实地址必须靠注入后的运行时工具取。

    同类名跨程序集时会**拒绝并列出候选**（`ok:false` + `candidates`），
    请改用完整名或补 `assembly=` —— 因为返回的是「这个类的全部字段表」，
    选错类不是少几条数据，而是整份结果都是别的类的。

    Args:
        full_name:         类的完整名（如 "PresetLoaderV2" 或
                           "TeamTapas.ProjectR.UserDatas.Service.UserData_Service"）。
        include_inherited: 是否包含继承来的字段/方法。
        method_name:       只关心某个方法时填这里。填了就只返回该方法的
                           全部重载（含 token / 参数表 / 是否静态），
                           等价于旧的 get_method_source，省一轮往返。
        assembly:          同名类消歧用（子串匹配，如 "TeamTapas"）。留空则
                           名字必须唯一，否则报错并给出候选。

    Returns:
        {ok, full_name, nested_name, namespace, image, token, ..., fields,
         methods, parent, base_chain, derived_count, derived, derived_returned,
         derived_has_more}
        `derived_count` 是**派生类总数**（真值）；`derived` 只是前 50 个名字，
        被截断时 `derived_has_more` 为 True —— 需要完整列表请用
        `find_derived_classes`。
    """
    idx, err = _need_index()
    if err:
        return err
    if not full_name:
        return _err("必须提供 full_name")
    # 指定 method_name 时强制带上继承的方法：被合并的 get_method_source
    # 原本就是 include_inherited=True，若不这样，查一个继承来的
    # Update/OnEnable 会得到「未找到」的假否定。
    want_inherited = include_inherited or bool(method_name)
    try:
        res = idx.class_detail(full_name, include_inherited=want_inherited,
                               assembly=assembly)
    except Exception as exc:
        return _err(f"查询失败：{exc}")

    # 只要某个方法时，在这里收窄 —— 合并 get_method_source 的关键：
    # 它原本就是「class_detail 结果里按名字过滤 methods」，独立成一个工具
    # 只会让 Agent 犹豫该调哪个。
    if method_name:
        if not res.get("found"):
            return _index_query(
                res, f"方法：{full_name}.{method_name}",
                hint="用 search_classes 先确认类名",
            )
        all_methods = res.get("methods") or []
        hits = [x for x in all_methods if x.get("name") == method_name]
        if not hits:
            return _err(
                f"类 {full_name} 中未找到方法 {method_name}",
                available=sorted({x.get("name", "") for x in all_methods})[:80],
                overload_count=0,
            )
        out = _ok(
            class_full_name=res.get("full_name"),
            method=method_name,
            overload_count=len(hits),
            methods=hits,
        )
        if len(hits) == 1:
            # 唯一匹配：把常用字段提上来，避免 Agent 写 methods[0].token
            for k in ("index", "name", "token", "return_type",
                      "parameter_count", "parameters", "is_static",
                      "is_virtual", "is_constructor", "flags"):
                if k in hits[0]:
                    out.setdefault(k, hits[0][k])
        else:
            out["note"] = (
                f"{method_name} 有 {len(hits)} 个重载，请按 token 选定目标"
                f"（把对应 token 传给 hook_method 的 token 参数）；"
                f"hook_method 的 args_type 也要与所选重载的参数对应。"
            )
        return out

    return _index_query(
        res, f"类：{full_name}",
        hint="用 search_classes 先确认名称；注意有些类型需要完整命名空间",
    )


@mcp.tool(annotations=_ann("搜索字段", read_only=True))
@_cap_response
def search_fields(
    pattern: str,
    offset: int = 0,
    limit: int = 100,
    owner: str = "",
    assembly: str = "",
) -> dict:
    """按名字搜索字段，返回所属类与字段类型。

    找「存档里的金币」「血量」这类目标时很有效。

    Args:
        pattern: 字段名子串（大小写不敏感）。
        offset:  跳过前多少条（配合 total / has_more 翻页）。
        limit:   本页最多返回多少条（上限 2000）。
        owner:   限定在某一个类里搜。接受完整类名或短名，但**短名必须唯一**：
                 若命中多个同名类（常见于不同程序集），本工具会拒绝并返回
                 candidates，此时请改用完整类名或用 assembly 消歧。
                 为什么不猜：把两个无关类的字段混在一起返回，ok 仍是 true，
                 但算出来的偏移必然错，且要到运行时才暴露。
        assembly: 程序集名子串，仅用于在 owner 命中多个类时消歧。

    Returns:
        {ok, handle, total, offset, limit, returned, has_more, items}
        `total` 是**真实匹配总数**；若超过句柄上限会带 handle_truncated
        与 handle_note（翻页请用本工具的 offset，不要依赖句柄拿全集）。
    """
    idx, err = _need_index()
    if err:
        return err
    if not pattern:
        return _err("必须提供 pattern")
    try:
        res = idx.search_fields(pattern=pattern, offset=offset, limit=limit,
                                owner=owner, assembly=assembly)
    except Exception as exc:
        return _err(f"搜索失败：{exc}")
    # 索引层在 owner 查不到/有歧义时返回 {"error": ...} 而不是结果集；
    # 若直接 _ok(**res) 就会变成 ok=true + error=... 的假成功。
    if "error" in res:
        return _err(res["error"], **{k: v for k, v in res.items() if k != "error"})
    return _ok(**res)


@mcp.tool(annotations=_ann("搜索方法", read_only=True))
@_cap_response
def search_methods(
    pattern: str,
    offset: int = 0,
    limit: int = 100,
    owner: str = "",
    assembly: str = "",
) -> dict:
    """按名字搜索方法，返回所属类、返回类型与 token。

    Args:
        pattern: 方法名子串（大小写不敏感）。
        offset:  跳过前多少条（配合 total / has_more 翻页）。
        limit:   本页最多返回多少条（上限 2000）。
        owner:   限定在某一个类里搜，语义与 search_fields 一致
                 （短名必须唯一，否则拒绝并给 candidates）。
        assembly: 程序集名子串，仅用于 owner 消歧。

    Returns:
        {ok, handle, total, offset, limit, returned, has_more, items}
        `total` 是真实匹配总数。
    """
    idx, err = _need_index()
    if err:
        return err
    if not pattern:
        return _err("必须提供 pattern")
    try:
        res = idx.search_methods(pattern=pattern, offset=offset, limit=limit,
                                 owner=owner, assembly=assembly)
    except Exception as exc:
        return _err(f"搜索失败：{exc}")
    if "error" in res:
        return _err(res["error"], **{k: v for k, v in res.items() if k != "error"})
    return _ok(**res)


@mcp.tool(annotations=_ann("查看类继承关系", read_only=True))
@_cap_response
def class_hierarchy(full_name: str, direction: str = "both", depth: int = 6,
                    assembly: str = "") -> dict:
    """查看类的继承体系（向上找基类、向下找派生类）。

    同类名跨程序集时会拒绝并给出候选（与 `class_detail` 同一规则）。

    Args:
        full_name: 类完整名。
        direction: up / down / both。
        depth:     向下递归深度。
        assembly:  同名类消歧用（子串匹配，如 "TeamTapas"）。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.class_hierarchy(full_name, direction=direction, depth=depth,
                                  assembly=assembly)
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    return _index_query(res, f"类：{full_name}")


@mcp.tool(annotations=_ann("查找继承自某类的类型", read_only=True))
@_cap_response
def find_derived_classes(base_full_name: str, limit: int = 200,
                         assembly: str = "") -> dict:
    """找出所有继承自某个基类的类型（含间接继承）。

    两个最常见的用法：

    · 找游戏自己写的 MonoBehaviour 脚本（Mod 最常 Hook 的目标）：
      `find_derived_classes(base_full_name="UnityEngine.MonoBehaviour",
                            assembly="Assembly-CSharp")`
      传 assembly 比按程序集名前缀猜「哪些是引擎自带的」可靠。
    · 找所有 UI 面板 / 数据服务：把 base_full_name 换成对应基类即可。

    基类**不必**在当前索引的程序集里（如 UnityEngine.MonoBehaviour
    属于框架程序集），这种情况结果依然有效，并在 note 里如实说明。

    `total` 是**匹配总数**，不受 limit 影响；`returned` 才是本次返回条数。
    若 `has_more` 为 True，说明被 limit 截断了，还有结果没返回。

    Args:
        base_full_name: 基类完整名，如 "UnityEngine.MonoBehaviour"。
        assembly:       只看该程序集（子串匹配，如 "TeamTapas"）。留空不限。
        limit:          最多返回多少条（`total` 仍是匹配总数）。

    Returns:
        {ok, found, base, total, returned, has_more, items: [{index,
         full_name, image, field_count, method_count}]}
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.find_derived_classes(base_full_name, limit=limit,
                                       assembly=assembly)
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    return _index_query(res, f"基类：{base_full_name}")


@mcp.tool(annotations=_ann("分析类型依赖", read_only=True))
@_cap_response
def analyze_dependency(full_name: str, assembly: str = "") -> dict:
    """分析一个类型引用了哪些其它类型（字段/父类/方法返回类型）。

    用来判断「改这个类会不会牵连很多地方」。

    同类名跨程序集时会拒绝并给出候选（与 `class_detail` 同一规则）。

    Args:
        full_name: 类完整名。
        assembly:  同名类消歧用（子串匹配，如 "TeamTapas"）。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.analyze_dependency(full_name, assembly=assembly)
    except Exception as exc:
        return _err(f"分析失败：{exc}")
    return _index_query(res, f"类：{full_name}")


@mcp.tool(annotations=_ann("列出程序集", read_only=True))
@_cap_response
def list_images(pattern: str = "", offset: int = 0, limit: int = 200) -> dict:
    """列出游戏里的程序集（dll）及各自的类型数量。

    用来判断「游戏逻辑主要在哪个 dll」，从而缩小搜索范围。

    Args:
        pattern: 名称子串过滤（如 "TeamTapas"）。
        offset:  从第几条开始（翻页用）。
        limit:   本页最多几条。被截断时 `has_more` 为 true。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        offset = max(0, offset)
        limit = max(1, min(limit, 2000))
        # total 用 COUNT(DISTINCT ...) 单独算，是真值；
        # items 才受 limit/offset 限制。以前只给 total 与 returned、
        # 不给 has_more 也没有 offset，调用方既无法确认被截断，也无法翻页。
        total = idx.conn.execute(
            "SELECT COUNT(DISTINCT image) FROM types WHERE image LIKE ?",
            (f"%{pattern}%",),
        ).fetchone()[0]
        rows = idx.conn.execute(
            "SELECT image, COUNT(*) AS n FROM types "
            "WHERE image LIKE ? GROUP BY image ORDER BY n DESC LIMIT ? OFFSET ?",
            (f"%{pattern}%", limit, offset),
        )
        items = [{"image": r["image"], "type_count": r["n"]} for r in rows]
        out = _ok(total=total, offset=offset, limit=limit,
                  returned=len(items), has_more=offset + len(items) < total,
                  items=items)
        if pattern and total == 0:
            out["note"] = (f"没有任何程序集名匹配 {pattern!r}；"
                           f"去掉 pattern 可看全部程序集。")
        return out
    except Exception as exc:
        return _err(f"查询失败：{exc}")


@mcp.tool(annotations=_ann("反编译 Mono 程序集", read_only=False, destroy=False, idempotent=True))
@_cap_response
def decompile(assembly: str = "", type_name: str = "", output_dir: str = "",
              split_files: bool = True) -> dict:
    """反编译 Mono/.NET 程序集，拿到**真正的 C# 源码**（外部工具 ilspycmd）。

    为什么需要它：静态元数据只能给出「有哪些类/字段/方法、签名如何」，
    但给不出**方法体逻辑**。想知道「这个方法到底怎么算伤害的」，
    就要反编译看源码 —— 这是元数据索引与运行时 Hook 都替代不了的一环。

    **只支持 Mono/.NET**。IL2CPP 的 GameAssembly.dll 里没有托管元数据，
    传进来会明确报错（IL2CPP 的 C# 反编译需要先造 DummyDll，本项目暂未实现）。

    两种粒度，用参数区分（故意只做一个工具，避免让你猜该用哪个）：

        · 反编译**一个类型**：给 type_name（可选 output_dir）
          源码小于 20 万字符时直接内联在 `source` 里返回。
        · 反编译**整个程序集**：只给 output_dir
          产物可能上万文件，因此只落盘 + 返回文件清单统计，**不内联源码**。

    Args:
        assembly:   程序集路径（.dll / .exe）。Mono 游戏通常是
                    `<游戏目录>/<游戏名>_Data/Managed/Assembly-CSharp.dll`。
                    留空则用当前会话识别出的那个（Mono 游戏可直接用）。
        type_name:  类型名，支持 `Namespace.Type`。传空表示反编译整个程序集。
                    嵌套类型可传 `Outer.Inner` 或 `Outer+Inner`：
                    由于 ilspycmd 无法单独定位嵌套类型，会自动改为反编译其
                    外层类型，并在 `nested_note` 里说明（`declared_types`
                    已核对，内层类型的声明确实在这份产物里）。
        output_dir: 输出目录。给了就落盘（整程序集模式**必填**）。
        split_files: 整程序集模式下是否一个类型一个文件（默认 True）。
                    True 出 800+ 个小文件（实测 3.5 秒），便于按需读取；
                    False 出单个大文件（实测 11.5 秒），便于整体搜索。
    """

    probe = dc.available()
    if not probe["available"]:
        return _err(probe["hint"])

    # 程序集留空 → 用当前会话识别出的那个（Mono 会话里就是 Assembly-CSharp.dll）
    asm = assembly
    if not asm:
        sess = state.state().summary()
        asm = (sess.get("metadata_path") or "")
        if not asm:
            return _err(
                "未指定 assembly，且当前会话也没有已打开的程序集。"
                "请传 assembly 路径，或先调用 open_game 选定游戏。"
            )
        if not str(asm).lower().endswith((".dll", ".exe")):
            return _err(
                f"当前会话的元数据不是 .NET 程序集（{asm}），无法反编译。"
                "decompile 只支持 Mono/.NET；IL2CPP 需要先造 DummyDll。"
            )

    try:
        if type_name:
            res = dc.decompile_type(asm, type_name, output_dir)
        elif output_dir:
            res = dc.decompile_assembly(asm, output_dir, split_files)
        else:
            return _err(
                "必须指定 type_name（反编译单个类型）或 output_dir"
                "（反编译整个程序集），两者至少要有一个。"
            )
    except Exception as exc:
        return _err(f"反编译失败：{exc}")

    if not res.get("ok"):
        return _err(res.get("error") or "反编译失败", **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    return _ok(**{k: v for k, v in res.items() if k != "ok"})


# ================================================================
# 4. 结果句柄层
# ================================================================


@mcp.tool(annotations=_ann("翻页/过滤读取查询结果", read_only=True))
@_cap_response
def read_query_result(handle: str, offset: int = 0, limit: int = 50,
                      pattern: str = "") -> dict:
    """按句柄翻页读取之前某次查询的完整结果；给了 `pattern` 就在结果内再过滤。

    列表类工具单次只返回一页，但完整结果留在句柄里。
    两个用途共用本工具：

    - 只看第 500~550 条 → 只给 `offset`/`limit`；
    - 在这一批结果里再找含某子串的 → 再加 `pattern`（大小写不敏感）。

    > 两者的成本**不一样**：纯翻页只解析取到的那一页（结果集是惰性物化的，
    > 见 `engine/index.py` 的 `_RowSource`）；而 `pattern` 要看每条的值才能
    > 过滤，必须把整个结果集解析一遍。所以在几万条的句柄上，
    > 纯翻页是毫秒级，带 `pattern` 会明显更慢 —— 缩小范围优先用
    > `search_*` 的参数（`pattern` / `subtype_of` / `owner` / `assembly`）。

    > 原先另有一个 `search_query_result`，实测它在 `pattern=""` 时与本工具
    > 输出完全相同 —— 它就是本工具的特例。按契约第 3 条（单一职责：
    > 一个是另一个的特例就合并）已删除，并入 `pattern` 参数。

    > **句柄有两个来源，本工具都能读**：`search_*` 这类静态查询登记的索引句柄，
    > 以及响应超限被截断时登记的 `cap-` 句柄（那种句柄可能在没有索引时也存在）。
    > 句柄只在**它所属的那一个会话**内有效；过期时返回 `available_handles`
    > 列出当前还能用的，绝不静默换成另一个结果集。

    Args:
        handle:  search_classes 等返回的 handle。
        offset:  起始位置。
        limit:   本页条数（上限 2000）。
        pattern: 在结果集内二次过滤的子串；留空表示不过滤（纯翻页）。
    """
    # 句柄有两个来源，都要能读（P2-4）：
    #   1. 索引句柄表 —— `search_*` 这类静态查询登记的；
    #   2. 会话级 cap 句柄表 —— 响应超限被截断时登记的完整列表。
    # 第 2 种可能在**没有索引**时也存在（纯运行时工具返回的大列表），
    # 所以这里不能一上来就 `_need_index()` 报错，否则那种句柄永远读不出来。
    st = state.state()
    idx = st.index
    res: dict | None = None
    if idx is not None:
        try:
            res = idx.read_query_result(handle, offset=offset, limit=limit,
                                        pattern=pattern)
        except Exception as exc:
            return _err(f"读取失败：{exc}")
        if res and res.get("found", True):
            out = _ok(**res)
            if pattern and not (res.get("items") or []):
                # 过滤零命中必须说清是「过滤没中」还是「句柄本身是空的」
                out["note"] = (f"句柄内共 {res.get('total', 0)} 条，但过滤条件 "
                               f"{pattern!r} 没有命中（或该页已越界）；"
                               f"去掉 pattern 可看全部。")
            return out

    qr = st.cap_results.get(handle)
    if qr is not None:
        out = _ok(found=True, **qr.page(offset, limit, pattern=pattern))
        if pattern and not (out.get("items") or []):
            out["note"] = (f"句柄内共 {out.get('total', 0)} 条，但过滤条件 "
                           f"{pattern!r} 没有命中（或该页已越界）；"
                           f"去掉 pattern 可看全部。")
        return out

    # 两个表都没有：如实报错，并把**两边**能用的句柄都给出来
    available = list((res or {}).get("available_handles") or [])
    available += [h for h in st.cap_results.handles() if h not in available]
    prefixes = []
    if idx is not None:
        prefixes.append(idx.results.prefix)
    prefixes.append(st.cap_results.prefix)
    return _err(
        f"句柄不存在或已过期：{handle}",
        available_handles=available,
        hint=("句柄只在**它所属的那一个会话**内有效：句柄前缀带引擎与代次"
              f"（当前会话前缀 {'、'.join(prefixes)}）。切换游戏或重建索引后"
              "旧句柄一律失效 —— 这是刻意的：否则旧句柄会静默读到"
              "另一个结果集（实测过，见 index.py 模块上方说明）"),
    )


# ================================================================
# 5. 运行时层
# ================================================================


@mcp.tool(annotations=_ann("注入游戏进程", read_only=False, destroy=False, idempotent=True, open_world=True))
@_cap_response
def inject_process(pid: int = 0, auto_find: bool = True,
                   preferred_module: str = "") -> dict:
    """把运行时探针注入正在运行的游戏进程。

    注入后才能使用 runtime_* / hook_* / drain_events 等在线工具。
    同一个进程只会注入一次（重复调用会复用，不会重复 Hook）。

    需要权限：Windows 上一般要管理员身份；游戏有反调试/反注入时会失败。

    Args:
        pid:             目标进程号。留空则自动找当前游戏的主程序。
        auto_find:       没给 pid 时是否自动查找。
        preferred_module: IL2CPP 模块名，默认自动识别
                          （GameAssembly.dll / libil2cpp.so）。

    Returns:
        {ok, pid, init: {module, base, api_available, api_missing, fallbacks}}
    """
    res = ensure_runtime(pid=pid, auto_find=auto_find,
                         preferred_module=preferred_module)
    if not res.get("ok"):
        return _err(res.get("error", "注入失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    # 把 pid 提到顶层，Agent 拿到结果就能直接用，不必再翻 runtime 子对象。
    # 注意：res 里本身可能带 pid 键，直接 _ok(pid=..., **res) 会
    # TypeError: got multiple values for keyword argument 'pid'，
    # 所以先把它摘掉再合并。
    rt = res.get("runtime") or {}
    body = {k: v for k, v in res.items() if k not in ("ok", "pid")}
    return _ok(pid=rt.get("pid") or pid, **body)


@mcp.tool(annotations=_ann("启动并注入游戏", read_only=False, destroy=False, open_world=True))
@_cap_response
def launch_game(exe: str = "", args: str = "", preferred_module: str = "") -> dict:
    """启动游戏并**在游戏初始化之前**注入探针。

    比先手动开游戏再 inject_process 更强：能在对象被创建之前就装好 Hook，
    适合拦截只执行一次的初始化逻辑。

    Args:
        exe:  游戏可执行文件路径。留空则用当前会话识别出的主程序。
        args: 附加命令行参数。按 Windows 习惯拆：空白分隔，双引号内保留空白
              （所以含空格的路径要写成 `-path "C:\\Games\\My Game\\a.txt"`）。
              反斜杠**不是**转义符，Windows 路径可以直接写。
    """
    target = exe
    if not target:
        st = state.state()
        target = st.exe
        if not target and st.game_dir:
            target = discovery.pick_main_exe(st.game_dir)
    if not target or not Path(target).is_file():
        return _err(f"可执行文件不存在：{target or '(未指定)'}",
                    hint="先用 open_game 或 inspect_game 确认主程序路径")

    argv = _split_argv(args) if args else []
    res = ensure_runtime(spawn=True, launcher_exe=target,
                         preferred_module=preferred_module)
    if not res.get("ok"):
        # 把下层的 hint 一起透传：例如「已有会话，先 close_session」
        # 这类出路信息比单纯的 error 更有用，丢掉会让 Agent 无从下手。
        extra = {k: v for k, v in res.items() if k not in ("ok", "error")}
        return _err(res.get("error", "启动失败"), target=target, argv=argv,
                    **extra)
    rt = res.get("runtime") or {}
    # 同 inject_process：res 里可能已有 pid/target/argv 键，先摘掉避免重复关键字
    body = {k: v for k, v in res.items()
            if k not in ("ok", "pid", "target", "argv")}
    return _ok(target=target, argv=argv, pid=rt.get("pid"), **body)


@mcp.tool(annotations=_ann("查看运行时概况", read_only=True, open_world=True))
@_cap_response
def runtime_info() -> dict:
    """查看已注入游戏的运行时概况：IL2CPP 模块、映像数量、可用 API。

    **一致性自检**：`image_count` 应与静态索引里的程序集数量吻合；
    不吻合说明探针挂到的可能不是同一个游戏实例，需要排查。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    try:
        ri = st.runtime.runtime_info()
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    out = dict(ri)
    # 附上静态侧的程序集数，方便直接比对
    if st.has_index():
        try:
            n = st.index.conn.execute(
                "SELECT COUNT(DISTINCT image) FROM types").fetchone()[0]
            out["static_image_count"] = n
            out["image_count_matches"] = (ri.get("image_count") == n)
        except Exception:
            pass
    return _ok(**out)


@mcp.tool(annotations=_ann("查询类的运行时信息", read_only=True, open_world=True))
@_cap_response
def runtime_class_info(
    name: str,
    namespace: str = "",
    image: str = "",
    fields: bool = True,
    methods: bool = True,
    method_filter: str = "",
) -> dict:
    """查类的**运行时**实况：字段的真实偏移与方法真实代码地址（RVA）。

    与静态 class_detail 的区别（很重要）：
        · 静态元数据里的字段**没有偏移**（偏移是运行时泛型实例化后才定的）
        · 静态元数据里的方法只有 token，**没有地址**
        · 这里给出的是运行时真实值 —— 这才是能直接用来 Hook 的落点

    **嵌套类型请传完整嵌套名**：IL2CPP 的 namespace 只属于最外层类型，
    嵌套类型（状态机 State、协程体 <Xxx>d__0、私有内部类）的命名空间是空的、
    只有短名。实测 IL2CPP 样本 20,072 个类型里 7,056 个（35%）是嵌套类型，
    名为 State 的类型有 131 个而只有 1 个是顶层。所以查嵌套类型时 `name`
    要写成完整嵌套名（如 "TeamTapas.ProjectR.UI2.RUI_FloatingUI.State"，
    从 search_classes 的 `nested_name` 直接抄）。只传短名而命中多个时，
    本工具返回 `ok: false` + `ambiguous: true` + `candidates`，不会替你猜。

    Args:
        name:          类名（短名；嵌套类型请传完整嵌套名）。
        namespace:     命名空间（可选，用于精确匹配）。
        image:         程序集名（可选，如 "TeamTapas.ProjectR.dll"）。
        method_filter: 只看名字含该子串的方法（类方法很多时用）。

    Returns:
        {ok, full_name, fields: [{name, type, offset, is_static}],
         methods: [{name, token, pointer, rva_hex, in_module, param_count}]}
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    try:
        res = st.runtime.class_info(
            namespace=namespace, name=name, image=image,
            fields=fields, methods=methods, method_filter=method_filter,
        )
        return res if res.get("ok") else _err(res.get("error", "查询失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"查询失败：{exc}")


@mcp.tool(annotations=_ann("定位方法的 Hook 落点", read_only=True, open_world=True))
@_cap_response
def find_hook_target(name: str, method: str, namespace: str = "",
                     image: str = "") -> dict:
    """定位某个方法可 Hook 的地址（含 RVA 与 token）。

    这是「从静态元数据到 Hook 落点」的关键一步：
    先用 search_classes/class_detail 从元数据确认目标方法（拿到 token），
    再用本工具在运行时把类实体化，拿到真实地址；
    最后用返回的 token 与静态 token 比对，确认挂的是同一个方法。

    返回的每个 candidate 里 `address` 可直接喂给 hook_address；
    `rva_hex` 可与静态元数据的 RVA 对照，`token` 用于确认是同一个重载。

    Args:
        name:      类名。
        method:    方法名（重载时看 candidates 的 param_count 区分）。
        namespace: 命名空间。
        image:     程序集名。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    try:
        res = st.runtime.method_target(namespace=namespace, name=name,
                                       method=method, image=image)
        if not res.get("ok"):
            return _err(res.get("error", "未找到"), **{
                k: v for k, v in res.items() if k not in ("ok", "error")
            })
        # 探针内部叫 pointer，而 hook_address 的入参叫 address。
        # 这里是两套命名唯一会碰面的地方，统一补一个 address 别名，
        # 免得 Agent 拿到 pointer 却不知道要传给 hook_address 的哪个参数。
        for cand in res.get("candidates") or []:
            if cand.get("pointer") and not cand.get("address"):
                cand["address"] = cand["pointer"]
        return _ok(**res)
    except Exception as exc:
        return _err(f"定位失败：{exc}")


@mcp.tool(annotations=_ann("Hook 方法", read_only=False, destroy=False, open_world=True))
@_cap_response
def hook_method(
    name: str,
    method: str,
    namespace: str = "",
    image: str = "",
    args_type: str = "",
    label: str = "",
    max_calls: int = 200,
    backtrace: bool = False,
    token: str = "",
    param_count: int = -1,
) -> dict:
    """在目标方法上安装 Hook，捕获它的调用（参数、调用来源、返回值）。

    **风险**：Hook 会改变游戏运行行为，且错误的参数类型可能崩溃游戏。
    建议先用 find_hook_target 确认落点与 token。

    **重载**：同名方法可能有多个重载，token 各不相同。若目标方法有多个
    重载而你又没给 `token` / `param_count`，本工具会**拒绝猜测**并返回
    候选清单（`ok: false`）。这是故意的：盲挂到错误的重载上，
    Hook 照样会成功，错误要到很久以后才以「数据不对」的形式暴露。
    最可靠的做法是从静态元数据取目标重载的 token 传进来。

    Args:
        name:      类名。
        method:    方法名。
        namespace: 命名空间。
        image:     程序集名。
        args_type: 参数类型列表，逗号分隔，用于解析参数值。
                   可选：int/uint/long/ulong/bool/float/double/ptr/string/cstring
                   例："ptr,int,float"。留空则不解析参数值。
        label:     自定义标签，便于在事件里识别（默认 "类名.方法名"）。
        max_calls: 最多记录多少次调用（防止高频方法刷爆事件队列）。
        backtrace: 是否记录调用栈（较慢，排障时用）。
        token:     目标重载的 token（如 "0x06000A3D"）。**选重载首选这个**，
                   静态元数据的 class_detail / get_method_source 里就有。
        param_count: 目标重载的参数个数（-1 表示不指定）。仅在拿不到 token
                   且各重载参数个数不同时可用。

    Returns:
        {ok, hook_id, class, method, chosen: {pointer, rva_hex, token},
         candidate_count, usable_count}
        失败时 {ok: false, error, candidates: [...]}。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    specs = [s.strip() for s in args_type.split(",") if s.strip()] if args_type else []
    try:
        res = st.runtime.hook_method(
            namespace=namespace, name=name, method=method, image=image,
            args=specs, label=label, max_calls=max_calls, backtrace=backtrace,
            token=token, param_count=param_count,
        )
        return res if res.get("ok") else _err(res.get("error", "Hook 失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"Hook 失败：{exc}")


@mcp.tool(annotations=_ann("在指定地址安装 Hook", read_only=False, destroy=False, open_world=True))
@_cap_response
def hook_address(
    address: str,
    args_type: str = "",
    label: str = "",
    max_calls: int = 200,
    retval_type: str = "ptr",
    backtrace: bool = False,
) -> dict:
    """在**任意地址**安装 Hook（不限于托管方法）。

    适用于：原生游戏函数、系统 API、以及从静态分析里算出的地址
    （模块基址 + RVA）。

    Args:
        address:    目标地址，十六进制（如 "0x7ffa05f7f050"）。
        args_type:  参数类型列表，逗号分隔（同 hook_method）。
        retval_type: 返回值类型（int/uint/float/double/ptr/string/bool）。
        max_calls:  最多记录多少次。
        backtrace:  是否记录调用栈。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    specs = [s.strip() for s in args_type.split(",") if s.strip()] if args_type else []
    try:
        res = st.runtime.hook(address=address, args=specs, label=label,
                              max_calls=max_calls, backtrace=backtrace,
                              retval_type=retval_type)
        if not res.get("ok"):
            return _err(res.get("error", "Hook 失败"), **{
                k: v for k, v in res.items() if k not in ("ok", "error")
            })
        # 探针把 RVA 信息放在 target_rva 子对象里（address/rva/rva_hex/module）。
        # 与 find_hook_target 保持一致，提到顶层，便于和静态元数据的 RVA 对照。
        tr = res.get("target_rva") or {}
        for k in ("module", "rva", "rva_hex"):
            if k in tr and k not in res:
                res[k] = tr[k]
        res.setdefault("address", res.get("target"))
        return _ok(**res)
    except Exception as exc:
        return _err(f"Hook 失败：{exc}")


@mcp.tool(annotations=_ann("卸载 Hook", read_only=False, destroy=True, idempotent=True, open_world=True))
@_cap_response
def unhook(hook_id: str = "") -> dict:
    """卸载 Hook。hook_id 留空则卸载全部。

    卸载后游戏行为恢复原状。收尾时务必调用（或用 close_session）。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        if hook_id:
            res = st.runtime.unhook(hook_id)
        else:
            res = st.runtime.unhook_all()
        return res if res.get("ok") else _err(res.get("error", "卸载失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"卸载失败：{exc}")


@mcp.tool(annotations=_ann("查看 Hook 状态", read_only=True, open_world=True))
@_cap_response
def hook_status() -> dict:
    """列出当前所有 Hook 及其触发次数。

    触发次数可以用来判断「Hook 装对了没」：
    如果挂的是每帧调用的方法却始终 0 次，多半是落点不对。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        return _ok(**st.runtime.hook_status())
    except Exception as exc:
        return _err(f"查询失败：{exc}")


@mcp.tool(annotations=_ann("读取 Hook 事件", read_only=False, destroy=True, open_world=True))
@_cap_response
def drain_events(limit: int = 200, kind: str = "") -> dict:
    """取走探针缓冲的 Hook 事件（调用记录）。

    语义与 AiMod 的 /v1/events?after= 一致：**取走即消费**。
    想持续观察就每隔一会儿调用一次，累加统计即可。

    Args:
        limit: 本次最多取多少条。
        kind:  只取某类事件（如 "hook"）。

    Returns:
        {ok, returned, pending_approx, dropped, events: [...]}
        事件形如 {type, id, label, phase: enter/leave, seq, thread,
                  from, args, retval}
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        return _ok(**st.runtime.drain_events(limit=limit, kind=kind))
    except Exception as exc:
        return _err(f"读取失败：{exc}")


@mcp.tool(annotations=_ann("读游戏内存", read_only=True, open_world=True))
@_cap_response
def read_memory(address: str, size: int = 16) -> dict:
    """读取游戏进程内存（十六进制返回）。

    用于：确认字段偏移对不对、读对象原始字节、观察内存变化。

    Args:
        address: 地址（十六进制，如 "0x1a2b3c4d"）。
        size:    读取字节数（上限 16 MB）。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        res = st.runtime.read_memory(address, size)
        return res if res.get("ok") else _err(res.get("error", "读取失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"读取失败：{exc}")


@mcp.tool(annotations=_ann("写游戏内存", read_only=False, destroy=True, idempotent=True, open_world=True))
@_cap_response
def write_memory(address: str, hex_data: str) -> dict:
    """写游戏进程内存。

    ⚠️ **危险操作**：写错地址会立刻崩溃游戏。
    写之前务必先用 read_memory 确认地址内容符合预期。
    建议只改浮点数/整数这类简单值，并先备份原值。

    Args:
        address:  地址（十六进制）。
        hex_data: 要写入的字节，十六进制串（如 "0000803f" 表示 float 1.0）。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        res = st.runtime.write_memory(address, hex_data)
        return res if res.get("ok") else _err(res.get("error", "写入失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"写入失败：{exc}")


@mcp.tool(annotations=_ann("读实例字段的值", read_only=True, open_world=True))
@_cap_response
def read_field_value(
    instance: str,
    name: str,
    field: str,
    namespace: str = "",
    image: str = "",
) -> dict:
    """读取某个对象实例上某个字段的原始值。

    自动完成三步：运行时查字段偏移 → 算出字段地址 → 读内存。

    **读几个字节由字段类型决定**（`_field_read_width`）：bool 读 1、int 读 4、
    long/double/引用类型读 8。原来固定读 8 字节，4 字节字段会连带读到**相邻
    字段**的 4 个字节 —— 既给出无意义的 `as_int64`（两个字段拼出来的数），
    也会在字段贴近页尾时把一次本可成功的读取变成假失败。

    返回里 `read_width` 是实际读取的字节数，解释字段也按宽度给：
    1 → `as_int8`/`as_uint8`/`as_bool`；2 → `as_int16`/`as_uint16`；
    4 → `as_int32`/`as_uint32`/`as_float`；8 → 再给 `as_int64`/`as_double`/`as_pointer`。

    局限：只适合**基础类型字段**（int/float/bool/enum 等）。
    引用类型字段读到的是指针，要读内容需再解引用（用 read_memory）。

    Args:
        instance:  对象指针（十六进制）。可从 Hook 事件的 args[0] 里拿到。
        name:      类名。
        field:     字段名。
        namespace: 命名空间。
        image:     程序集名。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process。")
    try:
        ci = st.runtime.class_fields(namespace=namespace, name=name,
                                     image=image, instance_only=True)
        if not ci.get("ok"):
            return _err(ci.get("error", "未找到类"), **{
                k: v for k, v in ci.items() if k not in ("ok", "error")
            })
        hit = None
        for f in ci.get("fields") or []:
            if f.get("name") == field:
                hit = f
                break
        if hit is None:
            return _err(
                f"类 {name} 里没有实例字段 {field}",
                available=[f.get("name") for f in (ci.get("fields") or [])],
            )
        off = hit.get("offset")
        if off is None:
            return _err(f"字段 {field} 没有运行时偏移（可能是静态字段）")

        # 字段地址 = 实例地址 + 偏移
        try:
            inst_val = int(instance, 16)
        except ValueError:
            return _err(f"instance 不是合法的十六进制地址：{instance}")
        field_addr = inst_val + int(off)

        # 按字段类型决定读几个字节：只读这个字段自己的字节，
        # 不把相邻字段的内容一起读进来（见 _field_read_width 的说明）。
        read_width = _field_read_width(hit.get("type"))
        raw = st.runtime.read_memory(hex(field_addr), read_width)
        if not raw.get("ok"):
            return _err(raw.get("error", "读取内存失败"),
                        field_address=hex(field_addr),
                        read_width=read_width)

        hexs = raw.get("hex") or ""
        out: dict[str, Any] = {
            "class": name,
            "field": field,
            "instance": instance,
            "offset": off,
            "field_address": hex(field_addr),
            "raw_hex": hexs,
            "field_type": hit.get("type"),
            "read_width": read_width,
        }
        # 按**实际读到的宽度**给解释，不给这个宽度下没有意义的解释：
        # 4 字节字段的 as_int64 是「本字段 + 下一个字段」拼出来的数，读它必然误判。
        import struct as _struct
        if len(hexs) >= 2:
            b1 = bytes.fromhex(hexs[:2])
            out["as_int8"] = _struct.unpack("<b", b1)[0]
            out["as_uint8"] = _struct.unpack("<B", b1)[0]
            out["as_bool"] = b1[0] != 0
        if len(hexs) >= 4:
            b2 = bytes.fromhex(hexs[:4])
            out["as_int16"] = _struct.unpack("<h", b2)[0]
            out["as_uint16"] = _struct.unpack("<H", b2)[0]
        if len(hexs) >= 8:
            b4 = bytes.fromhex(hexs[:8])
            out["as_int32"] = _struct.unpack("<i", b4)[0]
            out["as_uint32"] = _struct.unpack("<I", b4)[0]
            out["as_float"] = round(_struct.unpack("<f", b4)[0], 6)
        if len(hexs) >= 16:
            b8 = bytes.fromhex(hexs[:16])
            out["as_int64"] = _struct.unpack("<q", b8)[0]
            out["as_double"] = _struct.unpack("<d", b8)[0]
            out["as_pointer"] = hex(_struct.unpack("<Q", b8)[0])
        return _ok(**out)
    except Exception as exc:
        return _err(f"读取字段失败：{exc}")


@mcp.tool(annotations=_ann("内存特征码扫描", read_only=True, open_world=True))
@_cap_response
def scan_pattern(pattern: str, module: str = "", max_results: int = 32) -> dict:
    """在游戏模块里做 AOB（特征码）扫描。

    ⚠️ **这是兜底手段**。优先用元数据 + RVA 定位：
    本工具集的核心优势就是「不用扫内存也能找到 Hook 落点」。
    只有当符号被剥离、或目标是原生代码时，才用扫描。

    Args:
        pattern:     十六进制特征码，支持通配符 "??"。
                     例："48 8B ?? ?? E8"。
        module:      限定模块名（默认当前 IL2CPP 模块）。
        max_results: 最多返回多少个命中。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        res = st.runtime.scan_pattern(pattern, module=module,
                                      max_results=max_results)
        return res if res.get("ok") else _err(res.get("error", "扫描失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"扫描失败：{exc}")


@mcp.tool(annotations=_ann("在游戏内执行 JS", read_only=False, destroy=True, open_world=True))
@_cap_response
def eval_script(code: str) -> dict:
    """在游戏进程内执行 Frida JS 代码 —— 应急逃生舱。

    与 AiMod 的 eval_script 同定位：当预置工具不够用时，
    直接下发 JS 探测。可用全局对象：mod / api / funcs / hooks。

    **返回值必须是可 JSON 序列化的**（字符串/数字/布尔/数组/普通对象）。

    示例：
        "Process.arch"
        "mod.base.add(0x1000).toString()"
        "(function(){ return Process.enumerateModules().length; })()"

    Args:
        code: JS 表达式或语句块。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        res = st.runtime.eval_js(code)
        return res if res.get("ok") else _err(res.get("error", "执行失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"执行失败：{exc}")


@mcp.tool(annotations=_ann("开关 GC", read_only=False, destroy=False, idempotent=True, open_world=True))
@_cap_response
def gc_control(disable: bool = True) -> dict:
    """暂停/恢复游戏的 GC（垃圾回收）。

    Hook 期间建议暂停 GC，避免正在观察的对象被回收掉。
    **观察结束后务必恢复（disable=False）**，否则游戏内存会持续增长。

    Args:
        disable: True 暂停 GC，False 恢复。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        res = st.runtime.gc_control(disable=disable)
        return res if res.get("ok") else _err(res.get("error", "GC 控制失败"))
    except Exception as exc:
        return _err(f"GC 控制失败：{exc}")


# ================================================================
# 6. Unreal 引擎层
# ================================================================
#
# UE 没有 IL2CPP 那样的集中元数据文件，所以这一节的工具不是「查类型」，
# 而是「查资源容器」：pak / utoc 里到底打包了哪些资产。
# 已实测验证的能力（对两个真实样本与打包清单交叉校验 100% 命中）：
#   · 识别 UE 游戏
#   · 解析 .pak v11/v12 索引 → 完整文件清单
#   · 解析 .utoc v8 目录索引 → 完整文件清单（含 UTF-16 中文路径）
#   · 打包清单 Manifest_*.txt
#   · exe 内的 /Script/ 模块与对象路径
# **未实现**（诊断里会如实标注）：FName 池、UObject 属性偏移、.usmap。
# 这几项在静态样本里拿不到，需要运行时；不要暗示 Agent 它们可用。


def _ue_exe(exe: str) -> tuple[str, dict | None]:
    """解析出 UE 游戏的 exe：显式给了就用，否则从当前会话推。"""
    target = exe
    if not target:
        st = state.state()
        target = st.exe
        if not target and st.game_dir:
            ue = discovery.ue_paths(st.game_dir)
            target = ue.get("exe") or discovery.pick_main_exe(st.game_dir)
    if not target or not Path(target).is_file():
        return "", _err(
            f"找不到 UE 游戏主程序：{target or '(未指定)'}",
            hint="传 exe 参数，或先用 open_game/inspect_game 选定 UE 游戏",
        )
    return target, None


@mcp.tool(annotations=_ann("识别 Unreal 游戏", read_only=True))
@_cap_response
def detect_ue(exe: str = "") -> dict:
    """判断一个 exe 是否为 Unreal 引擎游戏，并给出可核查的证据。

    只判定**这一个**程序，不做全机扫描。

    **UE 版本号拿不到是正常的**：实测两个样本中 exe 内 UE 版本模式 0 命中，
    Engine/Build/Build.version 也不存在。此时返回空串 + 原因，不猜测。

    Args:
        exe: 游戏可执行文件路径。留空则用当前会话的主程序。
    """

    target, err = _ue_exe(exe)
    if err:
        return err
    try:
        res = ue_mod.detect_ue_game(target)
    except Exception as exc:
        return _err(f"识别失败：{exc}", exe=target)
    return _ok(**res)


@mcp.tool(annotations=_ann("提取 Unreal 游戏概况", read_only=False, destroy=False, idempotent=True))
@_cap_response
def ue_profile(exe: str = "", output_path: str = "") -> dict:
    """汇总一个 Unreal 游戏的可提取信息（资源容器、清单、脚本对象）。

    包含每个 .pak/.utoc 的文件数、目录数、以及条目样例。
    全量条目可能非常大（实测某样本 18,956 条 → JSON 15.8 MB），
    所以默认只给样例与计数；要完整清单用 ue_list_entries。

    Args:
        exe:         游戏可执行文件路径。留空则用当前会话的主程序。
        output_path: 可选，把完整 profile 写成 JSON 文件的路径。
    """

    target, err = _ue_exe(exe)
    if err:
        return err
    try:
        res = ue_mod.extract_ue_profile(
            target, output_path=output_path or None, include_entries=False,
        )
    except Exception as exc:
        return _err(f"提取失败：{exc}", exe=target)
    return _ok(**res)


@mcp.tool(annotations=_ann("列出 Unreal 资源容器条目", read_only=True))
@_cap_response
def ue_list_entries(container: str, kind: str = "auto",
                    pattern: str = "", limit: int = 200,
                    offset: int = 0) -> dict:
    """列出 .pak / .utoc 容器里的文件清单（可按名字过滤）。

    这是 UE 游戏上最实用的一步：想知道「这游戏有哪些资源、
    本地化文本在哪个文件里」，就靠它。

    Args:
        container: .pak 或 .utoc 文件路径。
        kind:      auto / pak / utoc。默认按扩展名自动判断。
        pattern:   名字子串过滤（大小写不敏感）。
        limit:     最多返回多少条。
        offset:    从第几条开始（配合 total 翻页）。
    """

    p = Path(container)
    if not p.is_file():
        return _err(f"文件不存在：{container}")
    k = kind
    if k == "auto":
        k = "utoc" if p.suffix.lower() == ".utoc" else "pak"

    try:
        if k == "utoc":
            # list_*_entries 在「解析失败」和「容器真的是空」两种情况下
            # 都会返回空列表。必须回到 read_*_index 看 ok/reason 才能区分，
            # 否则一个损坏/非 pak 的文件会被报成「解析成功但没条目」——
            # 假成功是最容易骗到 Agent 的一类结果。
            #
            # 把 report 传给 list_*_entries 复用，不要让它再解析一遍：
            # 实测同一容器解析两次占该工具耗时 48%~52%（大容器上过百毫秒）。
            report = ue_mod.read_iostore_index(str(p))
            if not report.get("ok"):
                return _err(
                    f"容器解析失败：{report.get('reason') or '未知原因'}",
                    container=str(p), kind=k,
                    file_size=report.get("file_size"),
                )
            entries = ue_mod.list_iostore_entries(str(p), report=report)
        elif k == "pak":
            report = ue_mod.read_pak_index(str(p))
            if not report.get("ok"):
                return _err(
                    f"容器解析失败：{report.get('reason') or '未知原因'}",
                    container=str(p), kind=k,
                    file_size=report.get("file_size"),
                )
            entries = ue_mod.list_pak_entries(str(p), report=report)
        else:
            return _err(f"kind 只能是 auto/pak/utoc，收到：{kind}")
    except Exception as exc:
        return _err(f"解析失败：{exc}", container=str(p), kind=k)

    total = len(entries)
    filtered_entries = entries
    if pattern:
        low = pattern.lower()
        filtered_entries = [
            e for e in entries if low in (e.get("path") or "").lower()
        ]

    matched = len(filtered_entries)
    limit = max(1, limit)
    page = filtered_entries[offset:offset + limit]
    # `returned` 与全项目其它列表工具（_paged / list_images / find_derived_classes）
    # 统一；以前这里叫 `count`，Agent 按惯例读 returned 只会拿到 None。
    out = _ok(container=str(p), kind=k, total=total,
              matched=matched, offset=offset, limit=limit,
              returned=len(page),
              has_more=offset + len(page) < matched, entries=page)
    if not page and total:
        # 容器有内容但这一页是空的：说清到底是哪种「空」
        if pattern and matched == 0:
            out["note"] = (f"容器解析成功（共 {total} 条），但过滤条件 "
                           f"{pattern!r} 没有命中任何条目")
        elif offset >= matched:
            # 关键分支：offset 越过的是**过滤后的**条数，不是总条数。
            # 以前判的是 offset >= total，于是 matched=1511 而 offset=6000
            # （6000 < 21439）时既不空说明也不给 has_more，
            # 调用方拿到空 entries 却毫无解释。
            out["note"] = (f"offset={offset} 已超出匹配到的 {matched} 条"
                           f"（容器共 {total} 条），这一页为空。"
                           f"请用 offset<{matched} 翻页。")
    elif total == 0:
        out["note"] = "容器解析成功，但里面确实没有任何条目。"
    return out


@mcp.tool(annotations=_ann("列出 Unreal 脚本对象", read_only=True))
@_cap_response
def ue_script_objects(exe: str = "", limit: int = 200) -> dict:
    """从 UE 游戏 exe 里提取 /Script/ 模块名与对象路径。

    这是**字符串证据**，不是反射元数据：能告诉你游戏大致有哪些系统
    （如 /Script/Engine、/Script/GameplayAbilities），
    但**拿不到父类链和属性偏移**——那需要运行时。

    Args:
        exe:   游戏可执行文件路径。留空则用当前会话的主程序。
        limit: 每类最多返回多少条。
    """

    target, err = _ue_exe(exe)
    if err:
        return err
    try:
        res = ue_mod.list_script_objects(target)
    except Exception as exc:
        return _err(f"提取失败：{exc}", exe=target)

    out = dict(res) if isinstance(res, dict) else {"result": res}
    # 结果可能很长，按 limit 截断，但保留原始计数
    for key in ("modules", "objects"):
        v = out.get(key)
        if isinstance(v, list):
            out[f"{key}_total"] = len(v)
            out[key] = v[:max(1, limit)]
    out.setdefault(
        "note",
        "这些是 exe 内的字符串证据，不是反射元数据；"
        "父类链与属性偏移无法由此获得。",
    )
    # 契约第 1 条：失败必须带 error。底层 ue.py 用的是 reason，
    # 这里转成约定的 error 字段（并保留 reason，不丢信息），
    # 否则按契约只读 error 的调用方会把失败当成正常返回。
    if not out.get("ok", True):
        reason = out.get("reason") or "未能在 exe 中提取到 /Script/ 对象"
        payload = {k: v for k, v in out.items() if k not in ("ok", "reason")}
        payload["reason"] = reason
        return _err(reason, **payload)
    return _ok(**out)


@mcp.tool(annotations=_ann("读取 Unreal 打包清单", read_only=True))
@_cap_response
def ue_manifest(path: str = "", game_dir: str = "", pattern: str = "",
                offset: int = 0, limit: int = 200) -> dict:
    """读取 UE 的打包清单 Manifest_UFSFiles_*.txt。

    清单是打包时生成的「应该有哪些文件」的真值，
    常用来校验从 pak/utoc 解析出的文件清单是否正确。

    Args:
        path:     清单文件路径。留空则自动在 game_dir 里找。
        game_dir: 游戏安装目录（path 留空时用）。
        pattern:  路径子串过滤。
        offset:   从第几条开始（翻页用）。
        limit:    本页最多多少条。被截断时 `has_more` 为 true。
    """

    target = path
    if not target:
        root = game_dir
        if not root:
            root = state.state().game_dir
        if not root:
            return _err("需要提供 path 或 game_dir")
        found = ue_mod.find_manifests(root)
        if not found:
            return _err(f"在 {root} 下没找到打包清单"
                        f"（Manifest_*Files*.txt）", game_dir=root)
        target = found[0]

    if not Path(target).is_file():
        return _err(f"文件不存在：{target}")
    try:
        res = ue_mod.read_manifest(target)
    except Exception as exc:
        return _err(f"读取失败：{exc}", path=target)

    if not isinstance(res, dict):
        res = {"manifest": res}
    entries = res.get("files") or res.get("entries") or []
    total = len(entries)
    if pattern:
        low = pattern.lower()
        entries = [e for e in entries
                   if low in (str(e) if not isinstance(e, dict)
                              else str(e.get("path", ""))).lower()]
    res["total"] = total
    res["matched"] = len(entries)
    key = "files" if "files" in res else "entries"
    offset = max(0, offset)
    limit = max(1, limit)
    page = entries[offset:offset + limit]
    res["offset"] = offset
    res["limit"] = limit
    res["returned"] = len(page)
    # 如实标注截断：以前只把列表切一刀，Agent 无从判断是否还有后续
    res["has_more"] = offset + len(page) < len(entries)
    res[key] = page
    if pattern and not entries:
        # 过滤零命中必须给说明，不能只给一个空列表
        res["note"] = (f"清单解析成功（共 {total} 条），但过滤条件 {pattern!r} "
                       f"没有命中任何条目；去掉 pattern 可看全部。")
    return _ok(**res)


# ================================================================
# 7. 诊断层
# ================================================================


@mcp.tool(annotations=_ann("环境与能力诊断", read_only=True))
@_cap_response
def get_diagnostics(scan_processes: bool = False) -> dict:
    """检查运行环境：Python、Frida、各引擎解析器是否可用。

    开始工作前建议先调一次，明确「这台机器上能做什么」。
    如果某项显示不可用，对应的工具会失败——这是环境问题，不是工具 bug。

    Args:
        scan_processes: 是否顺带列出本机正在运行的游戏。默认 **False**。
            该扫描实测耗时约 2.7 秒，且与 `find_game_processes` 职责重复，
            所以默认不扫 —— 要列运行中的游戏请直接调 `find_game_processes`。
    """
    try:
        info = state.diagnostics(scan_processes=scan_processes)
        info["version"] = __version__
        return _ok(**info)
    except Exception as exc:
        return _err(f"诊断失败：{exc}")


# ================================================================
# 8. 资源（MCP resources）
# ================================================================
#
# 只做一个资源，理由写在模块 docstring 的「资源」一节：
# 工具已经能给出的一切都不该再开第二个出口（§3.1 第 3 条）。

_GUIDE_REL = Path("skills") / "gamemod-mod" / "SKILL.md"


def _guide_candidates() -> list[Path]:
    """`gamemod://guide` 的正文按此优先级去找。

    覆盖两种部署形态：
      · 在仓库里跑 —— `server.py` 往上两级就是仓库根；
      · 在别处跑但装过 skill —— skill 会被复制到 `~/.dsh/skills/`。
    """
    here = Path(__file__).resolve()
    out = [p / _GUIDE_REL for p in here.parents[1:3]]      # src/ 与 仓库根
    out.append(Path.home() / ".dsh" / _GUIDE_REL)
    return out


def guide_text() -> str:
    """`gamemod://guide` 的正文（纯函数，便于单测）。

    找不到文件时**不返回空串、也不假装成功**，而是如实列出找过的位置
    并给出退路（§3.1 第 4 条：找不到就明说）。
    """
    tried: list[str] = []
    for path in _guide_candidates():
        tried.append(str(path))
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError as exc:                              # 权限/占用等
            tried.append(f"（读取失败：{exc}）")
    listed = "\n".join(f"- {t}" for t in tried)
    return (
        "# GameMod MCP 使用指南（当前环境里找不到）\n"
        "\n"
        "这份资源的内容来自 `skills/gamemod-mod/SKILL.md`，"
        "但下面这些位置都没有它：\n"
        f"{listed}\n"
        "\n"
        "不影响使用：`instructions` 里已有七步工作流，各工具的说明也在"
        "各自 docstring 里。先 get_diagnostics 确认这台机器能做什么，"
        "再 open_game 选定游戏即可。\n"
    )


@mcp.resource(
    "gamemod://guide",
    name="gamemod_guide",
    title="GameMod MCP 使用指南",
    description="本 MCP 的完整工作流说明（与 skills/gamemod-mod/SKILL.md 同源）。",
    mime_type="text/markdown",
)
def guide_resource() -> str:
    """完整使用指南：七步工作流、配方、引擎差异、常见坑。"""
    return guide_text()


# ================================================================
# 入口
# ================================================================


def main() -> None:
    """以 stdio 方式启动 MCP 服务器（MCP 客户端的标准接入方式）。"""
    progress(f"GameMod MCP v{__version__} 启动（stdio）")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
