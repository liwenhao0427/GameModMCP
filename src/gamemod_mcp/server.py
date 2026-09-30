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
    1. 发现层      : detect_games / find_game_processes / inspect_game
    2. 会话层      : open_game / get_session / close_session
    3. 静态查询层  : search_classes / class_detail / search_fields /
                     search_methods / class_hierarchy / find_derived_classes /
                     find_monobehaviours / analyze_dependency / list_images
    4. 结果句柄层  : read_query_result / search_query_result
    5. 运行时层    : runtime_info / runtime_class_info / hook_method /
                     unhook / drain_events / read_field_value /
                     scan_pattern / eval_script / inject_process / launch_game
    6. 诊断层      : get_diagnostics

关键约定
------------------------------------------------------------
    · 所有工具返回 dict，且**必定**带 `ok` 字段（成功 True / 失败 False）。
    · 失败时带 `error` 字段说明原因，绝不返回看似成功的空数据。
    · 日志一律走 stderr（见 logutil），因为 stdout 是 JSON-RPC 通道。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .engine import discovery, state
from .engine.state import ensure_index, ensure_runtime, select_game

# ---------------------------------------------------------------- 服务器实例

mcp = FastMCP(
    "gamemod",
    instructions=(
        "GameMod MCP —— 游戏 Mod 开发的通用工具集。\n"
        "\n"
        "典型工作流（强烈建议按顺序走）：\n"
        "  1. detect_games()           找到本机安装的游戏\n"
        "  2. open_game(game_dir=...)  选定游戏并准备好元数据索引\n"
        "  3. search_classes(...)      搜索类，拿到字段偏移与方法 RVA\n"
        "  4. class_detail(...)        细看某个类的字段/方法/inheritance\n"
        "  5. launch_game / inject_process  启动或注入游戏\n"
        "  6. runtime_class_info(...)  取字段的**真实运行时偏移**与方法真实地址\n"
        "  7. hook_method(...)         在目标方法上装 Hook\n"
        "  8. drain_events()           读取 Hook 捕获到的调用事件\n"
        "\n"
        "核心思路：**先用静态元数据定位，再用运行时确认**。\n"
        "绝大多数 Hook 落点都能从元数据的 RVA 直接算出，不需要内存扫描；\n"
        "scan_pattern 只是符号被剥离时的兜底手段。\n"
        "\n"
        "注意：静态查询（第 3、4 步）不需要启动游戏；\n"
        "运行时工具（第 5~8 步）需要游戏可注入，且运行 agent 的进程\n"
        "要有足够权限（Windows 上通常需要管理员）。"
    ),
)


# ---------------------------------------------------------------- 小工具


def _ok(**kw: Any) -> dict:
    return {"ok": True, **kw}


def _err(msg: str, **kw: Any) -> dict:
    return {"ok": False, "error": msg, **kw}


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
    """
    if res is None:
        return _err(f"未找到{what}", **extra)
    if not res.get("found", True):
        reason = res.get("reason") or f"未找到{what}"
        return _err(reason, **{k: v for k, v in res.items()
                               if k not in ("found", "reason")}, **extra)
    return _ok(**res)


# ================================================================
# 1. 发现层
# ================================================================


@mcp.tool(
    annotations={"title": "探测已安装的游戏", "readOnlyHint": True},
)
def detect_games(limit: int = 50) -> dict:
    """扫描本机已安装的游戏（Steam 库 + 常见安装位置）。

    这是工作流的起点。返回的每一项里 `install_dir` 就是后续
    open_game 需要的参数。

    Args:
        limit: 最多返回多少个。

    Returns:
        {ok, count, games: [{name, install_dir, exe, appid, source, engine}]}
    """
    try:
        games = discovery.list_steam_games(limit=0)
    except Exception as exc:
        return _err(f"扫描 Steam 库失败：{exc}")

    out = []
    for g in games[: max(1, limit)]:
        rec = {
            "name": g.name,
            "install_dir": g.install_dir,
            "exe": g.exe,
            "appid": g.appid,
            "source": g.source,
        }
        # 顺便识别引擎，方便 Agent 直接决定用哪套工具
        try:
            rec["engine"] = discovery.detect_engine(g.install_dir)
        except Exception as exc:
            rec["engine"] = "unknown"
            rec["engine_error"] = str(exc)
        out.append(rec)

    return _ok(count=len(out), total_scanned=len(games), games=out)


@mcp.tool(annotations={"title": "列出运行中的游戏进程", "readOnlyHint": True})
def find_game_processes(name_filter: str = "", only_games: bool = True) -> dict:
    """列出正在运行的游戏进程，并识别其运行时（il2cpp / mono / 其它）。

    Args:
        name_filter: 进程名子串过滤（大小写不敏感）。
        only_games:  只要看起来像游戏的进程（默认 True）。

    Returns:
        {ok, count, processes: [{pid, name, exe_path, window_title,
                                 runtime, architectures}]}
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
                "architectures": p.architectures,
            }
            for p in procs
        ],
    )


@mcp.tool(annotations={"title": "探测游戏安装目录", "readOnlyHint": True})
def inspect_game(game_dir: str) -> dict:
    """探测一个游戏目录：识别引擎、找出主程序与关键文件。

    当 detect_games 没扫到你的游戏（比如非 Steam 安装）时，
    直接用这个工具传入安装目录即可。

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


@mcp.tool(annotations={"title": "分析可执行文件结构", "readOnlyHint": True})
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


@mcp.tool(annotations={"title": "选定游戏并准备索引"})
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


@mcp.tool(annotations={"title": "查看当前会话状态", "readOnlyHint": True})
def get_session() -> dict:
    """查看当前会话状态：选中的游戏、索引统计、运行时注入情况。

    排查「为什么查不到类 / Hook 不上」时，先看这个。
    """
    st = state.state()
    return _ok(**st.summary())


@mcp.tool(annotations={"title": "关闭会话"})
def close_session(close_runtime: bool = True, close_index: bool = True) -> dict:
    """关闭当前会话：卸载所有 Hook、断开注入、关闭索引。

    做完了就调用它，把游戏恢复原状（卸载 Hook 后游戏行为回到未修改状态）。
    """
    st = state.state()
    out: dict[str, Any] = {}
    if close_runtime:
        out["runtime"] = st.close_runtime()
    if close_index:
        st.close_index()
        out["index_closed"] = True
    return _ok(**out)


# ================================================================
# 3. 静态查询层
# ================================================================


@mcp.tool(annotations={"title": "搜索类/结构/枚举", "readOnlyHint": True})
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
    用 search_query_result 在结果内再过滤。

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


@mcp.tool(annotations={"title": "查看类的完整细节", "readOnlyHint": True})
def class_detail(full_name: str, include_inherited: bool = False) -> dict:
    """查看一个类的完整信息：字段（含类型）、方法（含 token）、继承链。

    **这是定位 Hook 落点的核心工具**：字段给出偏移、方法给出 token，
    再配合 runtime_class_info 就能拿到真实的运行时地址。

    Args:
        full_name:         类的完整名（如 "PresetLoaderV2" 或
                           "TeamTapas.ProjectR.UserDatas.Service.UserData_Service"）。
        include_inherited: 是否包含继承来的字段/方法。
    """
    idx, err = _need_index()
    if err:
        return err
    if not full_name:
        return _err("必须提供 full_name")
    try:
        res = idx.class_detail(full_name, include_inherited=include_inherited)
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    return _index_query(
        res, f"类：{full_name}",
        hint="用 search_classes 先确认名称；注意有些类型需要完整命名空间",
    )


@mcp.tool(annotations={"title": "搜索字段", "readOnlyHint": True})
def search_fields(
    pattern: str,
    limit: int = 100,
    owner: str = "",
) -> dict:
    """按名字搜索字段，返回所属类与字段类型。

    找「存档里的金币」「血量」这类目标时很有效。

    Args:
        pattern: 字段名子串（大小写不敏感）。
        owner:   限定在某个类里搜（子串匹配）。
        limit:   最多返回多少条。
    """
    idx, err = _need_index()
    if err:
        return err
    if not pattern:
        return _err("必须提供 pattern")
    try:
        res = idx.search_fields(pattern=pattern, limit=limit, owner=owner)
    except Exception as exc:
        return _err(f"搜索失败：{exc}")
    # 索引层在 owner 查不到时返回 {"error": ...} 而不是结果集；
    # 若直接 _ok(**res) 就会变成 ok=true + error=... 的假成功。
    if "error" in res:
        return _err(res["error"], **{k: v for k, v in res.items() if k != "error"})
    return _ok(**res)


@mcp.tool(annotations={"title": "搜索方法", "readOnlyHint": True})
def search_methods(
    pattern: str,
    limit: int = 100,
    owner: str = "",
) -> dict:
    """按名字搜索方法，返回所属类、返回类型与 token。

    Args:
        pattern: 方法名子串（大小写不敏感）。
        owner:   限定在某个类里搜（子串匹配）。
        limit:   最多返回多少条。
    """
    idx, err = _need_index()
    if err:
        return err
    if not pattern:
        return _err("必须提供 pattern")
    try:
        res = idx.search_methods(pattern=pattern, limit=limit, owner=owner)
    except Exception as exc:
        return _err(f"搜索失败：{exc}")
    if "error" in res:
        return _err(res["error"], **{k: v for k, v in res.items() if k != "error"})
    return _ok(**res)


@mcp.tool(annotations={"title": "查看类继承关系", "readOnlyHint": True})
def class_hierarchy(full_name: str, direction: str = "both", depth: int = 6) -> dict:
    """查看类的继承体系（向上找基类、向下找派生类）。

    Args:
        full_name: 类完整名。
        direction: up / down / both。
        depth:     向下递归深度。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.class_hierarchy(full_name, direction=direction, depth=depth)
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    return _index_query(res, f"类：{full_name}")


@mcp.tool(annotations={"title": "查找派生类", "readOnlyHint": True})
def find_derived_classes(base_full_name: str, limit: int = 200) -> dict:
    """找出所有继承自某个基类的类型。

    典型用途：找游戏里所有 MonoBehaviour、所有 UI 面板、所有数据服务。

    Args:
        base_full_name: 基类完整名，如 "UnityEngine.MonoBehaviour"。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.find_derived_classes(base_full_name, limit=limit)
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    return _index_query(res, f"基类：{base_full_name}")


@mcp.tool(annotations={"title": "查找 MonoBehaviour 子类", "readOnlyHint": True})
def find_monobehaviours(name_pattern: str = "", limit: int = 200) -> dict:
    """列出游戏里的 MonoBehaviour 子类（即挂在 GameObject 上的脚本组件）。

    这些是 Mod 最常 Hook 的目标：Update、Start、OnEnable 等。

    Args:
        name_pattern: 名称子串过滤。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.find_monobehaviours(name_pattern=name_pattern, limit=limit)
        return _ok(**res)
    except Exception as exc:
        return _err(f"查询失败：{exc}")


@mcp.tool(annotations={"title": "分析类型依赖", "readOnlyHint": True})
def analyze_dependency(full_name: str) -> dict:
    """分析一个类型引用了哪些其它类型（字段/父类/方法返回类型）。

    用来判断「改这个类会不会牵连很多地方」。

    Args:
        full_name: 类完整名。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.analyze_dependency(full_name)
    except Exception as exc:
        return _err(f"分析失败：{exc}")
    return _index_query(res, f"类：{full_name}")


@mcp.tool(annotations={"title": "列出程序集", "readOnlyHint": True})
def list_images(pattern: str = "", limit: int = 200) -> dict:
    """列出游戏里的程序集（dll）及各自的类型数量。

    用来判断「游戏逻辑主要在哪个 dll」，从而缩小搜索范围。

    Args:
        pattern: 名称子串过滤（如 "TeamTapas"）。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        rows = idx.conn.execute(
            "SELECT image, COUNT(*) AS n FROM types "
            "WHERE image LIKE ? GROUP BY image ORDER BY n DESC LIMIT ?",
            (f"%{pattern}%", max(1, min(limit, 2000))),
        )
        items = [{"image": r["image"], "type_count": r["n"]} for r in rows]
        total = idx.conn.execute(
            "SELECT COUNT(DISTINCT image) FROM types WHERE image LIKE ?",
            (f"%{pattern}%",),
        ).fetchone()[0]
        return _ok(total=total, returned=len(items), items=items)
    except Exception as exc:
        return _err(f"查询失败：{exc}")


@mcp.tool(annotations={"title": "取方法的静态信息", "readOnlyHint": True})
def get_method_source(full_name: str, method_name: str) -> dict:
    """取某个方法的静态信息（token、参数、返回类型、是否静态/虚方法）。

    注意：本工具**不返回 C# 源码**（IL2CPP 元数据里没有 IL 指令体）。
    Mono 游戏可以直接用 ILSpy/dnSpy 反编译拿源码。

    **重载**：同名方法可能有多个（参数不同），因此结果主体是 `methods` 列表。
    如果只有一个匹配，它的 token 等字段也会被提到顶层，省得 Agent 多下钻一层。

    Args:
        full_name:   类完整名。
        method_name: 方法名。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.get_method_source(full_name, method_name)
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    out = _index_query(res, f"方法：{full_name}.{method_name}")
    if out.get("ok"):
        ms = out.get("methods") or []
        out["overload_count"] = len(ms)
        if len(ms) == 1:
            # 唯一匹配：把常用字段提上来，避免 Agent 写 methods[0].token
            for k in ("index", "name", "token", "return_type",
                      "parameter_count", "parameters", "is_static",
                      "is_virtual", "is_constructor", "flags"):
                if k in ms[0]:
                    out.setdefault(k, ms[0][k])
        elif len(ms) > 1:
            out["note"] = (
                f"{method_name} 有 {len(ms)} 个重载，请按 parameter_count "
                f"选定目标；直接 hook_method 时也要给对参数类型。"
            )
    return out


# ================================================================
# 4. 结果句柄层
# ================================================================


@mcp.tool(annotations={"title": "翻页读取查询结果", "readOnlyHint": True})
def read_query_result(handle: str, offset: int = 0, limit: int = 50) -> dict:
    """按句柄翻页读取之前某次查询的完整结果。

    列表类工具单次只返回一页，但完整结果留在句柄里。
    需要看第 500~550 条就用这个。

    Args:
        handle: search_classes 等返回的 handle。
        offset: 起始位置。
        limit:  本页条数（上限 2000）。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.read_query_result(handle, offset=offset, limit=limit)
    except Exception as exc:
        return _err(f"读取失败：{exc}")
    if not res or not res.get("found", True):
        return _err(
            f"句柄不存在或已过期：{handle}",
            available_handles=(res or {}).get("available_handles", []),
            hint="句柄在会话内有效；切换游戏或重建索引后会失效",
        )
    return _ok(**res)


@mcp.tool(annotations={"title": "在查询结果内再过滤", "readOnlyHint": True})
def search_query_result(handle: str, pattern: str,
                        offset: int = 0, limit: int = 50) -> dict:
    """在某个查询句柄的结果集内部再做一次子串过滤。

    比重新查一遍库更省事，也避免丢掉原有上下文。

    Args:
        handle:  查询句柄。
        pattern: 过滤子串（大小写不敏感）。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.search_query_result(handle, pattern=pattern,
                                      offset=offset, limit=limit)
    except Exception as exc:
        return _err(f"过滤失败：{exc}")
    if not res or not res.get("found", True):
        return _err(
            f"句柄不存在或已过期：{handle}",
            available_handles=(res or {}).get("available_handles", []),
        )
    return _ok(**res)


# ================================================================
# 5. 运行时层
# ================================================================


@mcp.tool(annotations={"title": "注入游戏进程"})
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


@mcp.tool(annotations={"title": "启动并注入游戏"})
def launch_game(exe: str = "", args: str = "", preferred_module: str = "") -> dict:
    """启动游戏并**在游戏初始化之前**注入探针。

    比先手动开游戏再 inject_process 更强：能在对象被创建之前就装好 Hook，
    适合拦截只执行一次的初始化逻辑。

    Args:
        exe:  游戏可执行文件路径。留空则用当前会话识别出的主程序。
        args: 附加命令行参数（空格分隔）。
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

    argv = args.split() if args else []
    res = ensure_runtime(spawn=True, launcher_exe=target,
                         preferred_module=preferred_module)
    if not res.get("ok"):
        return _err(res.get("error", "启动失败"), target=target, argv=argv)
    rt = res.get("runtime") or {}
    # 同 inject_process：res 里可能已有 pid/target/argv 键，先摘掉避免重复关键字
    body = {k: v for k, v in res.items()
            if k not in ("ok", "pid", "target", "argv")}
    return _ok(target=target, argv=argv, pid=rt.get("pid"), **body)


@mcp.tool(annotations={"title": "查看运行时概况", "readOnlyHint": True})
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


@mcp.tool(annotations={"title": "查询类的运行时信息", "readOnlyHint": True})
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

    Args:
        name:          类名（短名）。
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


@mcp.tool(annotations={"title": "定位方法的 Hook 落点", "readOnlyHint": True})
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


@mcp.tool(annotations={"title": "Hook 方法"})
def hook_method(
    name: str,
    method: str,
    namespace: str = "",
    image: str = "",
    args_type: str = "",
    label: str = "",
    max_calls: int = 200,
    backtrace: bool = False,
) -> dict:
    """在目标方法上安装 Hook，捕获它的调用（参数、调用来源、返回值）。

    **风险**：Hook 会改变游戏运行行为，且错误的参数类型可能崩溃游戏。
    建议先用 find_hook_target 确认落点与 token。

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

    Returns:
        {ok, hook_id, class, method, chosen: {pointer, rva_hex, token}}
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
        )
        return res if res.get("ok") else _err(res.get("error", "Hook 失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"Hook 失败：{exc}")


@mcp.tool(annotations={"title": "在指定地址安装 Hook"})
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


@mcp.tool(annotations={"title": "卸载 Hook"})
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


@mcp.tool(annotations={"title": "查看 Hook 状态", "readOnlyHint": True})
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


@mcp.tool(annotations={"title": "读取 Hook 事件", "readOnlyHint": True})
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


@mcp.tool(annotations={"title": "读游戏内存", "readOnlyHint": True})
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


@mcp.tool(annotations={"title": "写游戏内存"})
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


@mcp.tool(annotations={"title": "读实例字段的值", "readOnlyHint": True})
def read_field_value(
    instance: str,
    name: str,
    field: str,
    namespace: str = "",
    image: str = "",
) -> dict:
    """读取某个对象实例上某个字段的原始值。

    自动完成三步：运行时查字段偏移 → 算出字段地址 → 读内存。
    返回原始字节与常见解释（int / float）。

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
        import ctypes
        try:
            inst_val = int(instance, 16)
        except ValueError:
            return _err(f"instance 不是合法的十六进制地址：{instance}")
        field_addr = inst_val + int(off)

        raw = st.runtime.read_memory(hex(field_addr), 8)
        if not raw.get("ok"):
            return _err(raw.get("error", "读取内存失败"),
                        field_address=hex(field_addr))

        hexs = raw.get("hex") or ""
        out: dict[str, Any] = {
            "class": name,
            "field": field,
            "instance": instance,
            "offset": off,
            "field_address": hex(field_addr),
            "raw_hex": hexs,
            "field_type": hit.get("type"),
        }
        # 给出常见解释，方便判断
        import struct as _struct
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


@mcp.tool(annotations={"title": "内存特征码扫描", "readOnlyHint": True})
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


@mcp.tool(annotations={"title": "在游戏内执行 JS"})
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


@mcp.tool(annotations={"title": "开关 GC", "readOnlyHint": False})
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


@mcp.tool(annotations={"title": "识别 Unreal 游戏", "readOnlyHint": True})
def detect_ue(exe: str = "") -> dict:
    """判断一个 exe 是否为 Unreal 引擎游戏，并给出可核查的证据。

    与 detect_games 的区别：那个是「扫本机装了什么」，
    这个是「判定这一个具体程序是不是 UE」。

    **UE 版本号拿不到是正常的**：实测两个样本中 exe 内 UE 版本模式 0 命中，
    Engine/Build/Build.version 也不存在。此时返回空串 + 原因，不猜测。

    Args:
        exe: 游戏可执行文件路径。留空则用当前会话的主程序。
    """
    from .engine import ue as ue_mod

    target, err = _ue_exe(exe)
    if err:
        return err
    try:
        res = ue_mod.detect_ue_game(target)
    except Exception as exc:
        return _err(f"识别失败：{exc}", exe=target)
    return _ok(**res)


@mcp.tool(annotations={"title": "提取 Unreal 游戏概况", "readOnlyHint": True})
def ue_profile(exe: str = "", output_path: str = "") -> dict:
    """汇总一个 Unreal 游戏的可提取信息（资源容器、清单、脚本对象）。

    包含每个 .pak/.utoc 的文件数、目录数、以及条目样例。
    全量条目可能非常大（实测某样本 18,956 条 → JSON 15.8 MB），
    所以默认只给样例与计数；要完整清单用 ue_list_entries。

    Args:
        exe:         游戏可执行文件路径。留空则用当前会话的主程序。
        output_path: 可选，把完整 profile 写成 JSON 文件的路径。
    """
    from .engine import ue as ue_mod

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


@mcp.tool(annotations={"title": "列出 Unreal 资源容器条目", "readOnlyHint": True})
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
    from .engine import ue as ue_mod

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
            report = ue_mod.read_iostore_index(str(p))
            if not report.get("ok"):
                return _err(
                    f"容器解析失败：{report.get('reason') or '未知原因'}",
                    container=str(p), kind=k,
                    file_size=report.get("file_size"),
                )
            entries = ue_mod.list_iostore_entries(str(p))
        elif k == "pak":
            report = ue_mod.read_pak_index(str(p))
            if not report.get("ok"):
                return _err(
                    f"容器解析失败：{report.get('reason') or '未知原因'}",
                    container=str(p), kind=k,
                    file_size=report.get("file_size"),
                )
            entries = ue_mod.list_pak_entries(str(p))
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
    page = filtered_entries[offset:offset + max(1, limit)]
    out = _ok(container=str(p), kind=k, total=total,
              matched=matched, offset=offset, count=len(page), entries=page)
    if not page and total:
        # 容器有内容但这一页是空的：说清是「过滤没命中」还是「翻页翻过头了」
        if pattern and matched == 0:
            out["note"] = (f"容器解析成功（共 {total} 条），但过滤条件 "
                           f"{pattern!r} 没有命中任何条目")
        elif offset >= total:
            out["note"] = (f"offset={offset} 已超出总数 {total}，"
                           f"这一页为空。请用 offset<{total} 翻页。")
    elif total == 0:
        out["note"] = "容器解析成功，但里面确实没有任何条目。"
    return out


@mcp.tool(annotations={"title": "列出 Unreal 脚本对象", "readOnlyHint": True})
def ue_script_objects(exe: str = "", limit: int = 200) -> dict:
    """从 UE 游戏 exe 里提取 /Script/ 模块名与对象路径。

    这是**字符串证据**，不是反射元数据：能告诉你游戏大致有哪些系统
    （如 /Script/Engine、/Script/GameplayAbilities），
    但**拿不到父类链和属性偏移**——那需要运行时。

    Args:
        exe:   游戏可执行文件路径。留空则用当前会话的主程序。
        limit: 每类最多返回多少条。
    """
    from .engine import ue as ue_mod

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
    return _ok(**out)


@mcp.tool(annotations={"title": "读取 Unreal 打包清单", "readOnlyHint": True})
def ue_manifest(path: str = "", game_dir: str = "", pattern: str = "",
                limit: int = 200) -> dict:
    """读取 UE 的打包清单 Manifest_UFSFiles_*.txt。

    清单是打包时生成的「应该有哪些文件」的真值，
    常用来校验从 pak/utoc 解析出的文件清单是否正确。

    Args:
        path:     清单文件路径。留空则自动在 game_dir 里找。
        game_dir: 游戏安装目录（path 留空时用）。
        pattern:  路径子串过滤。
        limit:    最多返回多少条。
    """
    from .engine import ue as ue_mod

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
    res[key] = entries[:max(1, limit)]
    return _ok(**res)


# ================================================================
# 7. 诊断层
# ================================================================


@mcp.tool(annotations={"title": "环境与能力诊断", "readOnlyHint": True})
def get_diagnostics() -> dict:
    """检查运行环境：Python、Frida、各引擎解析器是否可用、本机有无游戏在跑。

    开始工作前建议先调一次，明确「这台机器上能做什么」。
    如果某项显示不可用，对应的工具会失败——这是环境问题，不是工具 bug。
    """
    try:
        info = state.diagnostics()
        info["version"] = __version__
        return _ok(**info)
    except Exception as exc:
        return _err(f"诊断失败：{exc}")


# ================================================================
# 入口
# ================================================================


def main() -> None:
    """以 stdio 方式启动 MCP 服务器（MCP 客户端的标准接入方式）。"""
    from .logutil import progress
    progress(f"GameMod MCP v{__version__} 启动（stdio）")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
