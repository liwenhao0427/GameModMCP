"""会话状态：把「发现游戏 → 建索引 → 注入运行时」串成一条可复用的会话。

为什么要单独做这一层
--------------------------------------------------
MCP 工具是无状态的函数调用，但 Mod 开发天然是有状态的：
Agent 先选定一个游戏，然后反复查询它的类、Hook 它的方法。
如果每个工具调用都重新解析元数据、重新注入进程，既慢又会重复 Hook。

因此这里维护一个**进程级单例会话**，记录：
    · 当前选中的游戏目录与引擎类型
    · 已打开的元数据索引（含查询结果句柄表）
    · 已注入的运行时会话（Frida）
并把「当前游戏」作为所有工具的隐式上下文。

与 AiMod 的对应关系
--------------------------------------------------
AiMod 的流程是：detecting → reuse/injecting → verify → mono_snapshot →
mcp_start → final_verify，并且有 session_lock 保证单会话。
本模块的 `ensure_*` 系列方法扮演同样角色，只是把「云 + 订阅校验」去掉，
换成纯本地、可自由重入的实现。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from ..logutil import progress, warn
from . import discovery
from .il2cpp_meta import MetadataError
from .index import Index, ResultStore
from .pe import summarize as pe_summarize

# ---------------------------------------------------------------- 状态对象


class SessionState:
    """当前会话状态（进程内单例）。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.game_dir: str = ""
        self.engine: str = ""
        self.exe: str = ""
        self.metadata_path: str = ""
        self._index: Index | None = None
        # 响应上限（P2-4）截断下来的完整列表放这里，而不是放进 Index 的句柄表：
        # 那样的话「没有索引」的工具（纯运行时那几个）就没有句柄可用，
        # 而这里的东西必须在任何工具下都能登记。
        # `close_index` 会清掉它，于是「切游戏 ⇒ 旧句柄全失效」是一条统一规则。
        self.cap_results = ResultStore(owner="cap")
        self.runtime = None            # RuntimeSession 或 None
        self.notes: list[str] = []     # 操作留痕，便于 Agent 自查
        self.opened_at: float = 0.0

    # ------------------------------------------------ 留痕

    def note(self, text: str) -> None:
        self.notes.append(f"[{time.strftime('%H:%M:%S')}] {text}")
        if len(self.notes) > 200:
            del self.notes[:100]

    # ------------------------------------------------ 索引

    @property
    def index(self) -> Index | None:
        return self._index

    def has_index(self) -> bool:
        return self._index is not None

    def close_index(self) -> dict:
        """关闭索引。**返回结果**（以前只 warn，失败对上层不可见）。"""
        # 索引句柄与 cap 句柄一起作废：切游戏后旧句柄一律失效是一条统一规则，
        # 不能让「cap 句柄」成为例外（它的数据同样属于上一个游戏）。
        # 注意放在 early-return **之前** —— 纯运行时工具也会往这里登记句柄，
        # 那时 _index 可能本来就是 None。
        self.cap_results.clear()
        if self._index is None:
            return {"ok": True, "already_closed": True}
        try:
            self._index.close()
        except Exception as exc:
            warn(f"关闭索引失败：{exc}")
            # 失败时不摘引用，便于重试；同时把原因交给上层
            return {"ok": False, "error": f"关闭索引失败：{exc}"}
        self._index = None
        return {"ok": True}

    # ------------------------------------------------ 运行时

    def has_runtime(self) -> bool:
        return self.runtime is not None and getattr(self.runtime, "alive", False)

    def close_runtime(self) -> dict:
        if self.runtime is None:
            return {"ok": True, "already_closed": True}
        try:
            res = self.runtime.close()
        except Exception as exc:
            warn(f"关闭运行时会话失败：{exc}")
            # 失败时**保留** self.runtime：会话可能只是没 detach 干净，
            # 保留引用调用方才能重试。以前这里用 finally 无条件清空，
            # 于是「关闭失败」之后连重试的入口都没有了。
            return {"ok": False, "error": str(exc)}
        if res.get("ok"):
            self.runtime = None
        else:
            warn(f"运行时会话未完全关闭：{res.get('error')}")
        return res

    # ------------------------------------------------ 概要

    def summary(self) -> dict[str, Any]:
        rt = None
        if self.runtime is not None:
            rt = {
                "pid": self.runtime.pid,
                "name": self.runtime.name,
                "alive": self.runtime.alive,
                "spawned": self.runtime.spawned,
            }
        idx_info = None
        if self._index is not None:
            try:
                idx_info = self._index.index_info()
            except Exception as exc:
                idx_info = {"error": str(exc)}
        return {
            "game_dir": self.game_dir,
            "engine": self.engine,
            "engine_label": _engine_label(self.engine),
            "exe": self.exe,
            "metadata_path": self.metadata_path,
            "index_ready": self.has_index(),
            "index": idx_info,
            "runtime": rt,
            "runtime_ready": self.has_runtime(),
            "notes": self.notes[-20:],
        }


_state = SessionState()


# 引擎名 → 给 Agent 看的说明。这份说明会随 open_game 一起返回，
# 让 Agent 一开始就知道该引擎「元数据里有什么、没有什么」。
_ENGINE_LABELS = {
    "il2cpp": "IL2CPP (Unity AOT) — 逻辑在 GameAssembly.dll，元数据是 global-metadata.dat",
    "mono": "Mono (Unity JIT / .NET) — 逻辑在托管 dll 里，可直接反编译成 C#",
    "ue": "Unreal Engine — 无集中元数据，结构信息依赖 UE profile / .usmap",
    "unity": "Unity（未能进一步区分 IL2CPP 还是 Mono）",
}


def _engine_label(engine: str) -> str:
    """引擎的可读说明；未知引擎原样返回，不编。"""
    return _ENGINE_LABELS.get(engine, engine or "未识别")


def state() -> SessionState:
    """取当前会话状态。"""
    return _state


# ---------------------------------------------------------------- 选择游戏


def _same_path(a: str, b: str) -> bool:
    """比较两个路径是否指向同一个文件/目录（容错大小写与相对路径）。"""
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.abspath(a)) == \
               os.path.normcase(os.path.abspath(b))
    except Exception:
        return False


def select_game(
    game_dir: str = "",
    engine: str = "",
    metadata_path: str = "",
    exe: str = "",
) -> dict:
    """选定一个游戏作为后续操作的上下文。

    允许只给 game_dir（自动识别引擎），也允许直接给 metadata_path
    （适合只做离线分析、不装游戏的场景）。

    **重复选中同一个游戏不会重建会话** —— 否则每次 open_game
    都会把已经建好的索引和已注入的探针扔掉，Agent 反复调用就会反复重建。
    """
    with _state.lock:
        # 先判断这次是不是在选同一个游戏；是的话直接复用，什么都不动
        want_meta = metadata_path
        want_dir = game_dir
        if not want_meta and want_dir:
            # 目录模式下先解析出元数据路径，才能判断是否同一个游戏
            try:
                det = engine or discovery.detect_engine(want_dir)
                if det == "il2cpp":
                    want_meta = discovery.il2cpp_paths(want_dir).get("metadata", "")
                elif det == "mono":
                    # Mono 的索引对象是单个程序集，同样要解析出来才能比对
                    mono = discovery.mono_paths(want_dir)
                    want_meta = _pick_mono_assembly(mono.get("assemblies") or [])
            except Exception:
                want_meta = ""

        # 复用条件：元数据文件相同 **且** 引擎没被指定成另一个。
        # 只比路径是不够的 —— 同一个文件可以被两种解析器读，而两种读法
        # 给出的结果完全不同（IL2CPP 器读 Mono 程序集只会得到空结果）。
        want_engine = (engine or "").strip().lower()
        same_engine = (not want_engine
                       or want_engine == _state.engine
                       or want_engine in ("auto", "unknown"))
        if want_meta and _same_path(want_meta, _state.metadata_path) \
                and _state.has_index() and same_engine:
            _state.note("重复选中同一个游戏，复用现有会话")
            out = {"ok": True, "reused": True, **_state.summary()}
            return out

        # 切换游戏时，先收拾掉上一个会话的资源，避免泄漏与串数据
        _state.close_runtime()
        _state.close_index()
        _state.notes.clear()

        if metadata_path:
            mp = Path(metadata_path)
            if not mp.is_file():
                return {"ok": False,
                        "error": f"元数据文件不存在：{metadata_path}"}
            _state.metadata_path = str(mp)
            _state.game_dir = str(mp.parent)
            # 显式指定引擎就用它；否则按文件内容嗅探（global-metadata.dat 的
            # magic vs .NET 程序集的 MZ 头），不要一律当成 il2cpp。
            _state.engine = engine or _sniff_engine_for(mp)
            _state.exe = exe
            _state.opened_at = time.time()
            _state.note(f"选定元数据文件 {mp.name}（引擎 {_state.engine}）")
            return {"ok": True, **_state.summary()}

        if not game_dir:
            return {"ok": False, "error": "必须提供 game_dir 或 metadata_path"}

        gd = Path(game_dir)
        if not gd.is_dir():
            return {"ok": False, "error": f"游戏目录不存在：{game_dir}"}

        detected = engine or discovery.detect_engine(gd)
        _state.game_dir = str(gd)
        _state.engine = detected
        _state.opened_at = time.time()

        # 按引擎类型定位关键文件
        if detected == "il2cpp":
            paths = discovery.il2cpp_paths(gd)
            _state.metadata_path = paths.get("metadata", "")
            if not _state.metadata_path:
                # 允许用户手工指定（有些游戏目录结构不标准）
                return {
                    "ok": False,
                    "engine": detected,
                    "error": "识别为 IL2CPP，但没找到 global-metadata.dat。"
                             "请用 metadata_path 参数直接指定该文件的路径。",
                    "hint": discovery.il2cpp_paths(gd),
                }
        elif detected == "mono":
            mono = discovery.mono_paths(gd)
            managed = mono.get("managed_dir", "")
            _state.exe = managed or mono.get("exe", "")
            # Mono 的元数据单位是**单个程序集**而不是目录，
            # 所以这里挑出「游戏自己的代码」那个 dll 作为索引对象。
            # 通常就是 Assembly-CSharp.dll；找不到则退到最大的非框架程序集。
            _state.metadata_path = _pick_mono_assembly(mono.get("assemblies") or [])
            if not _state.metadata_path:
                return {
                    "ok": False,
                    "engine": detected,
                    "error": "识别为 Mono，但没找到可索引的托管程序集。"
                             "请用 metadata_path 参数直接指定某个 .dll。",
                    "hint": {"managed_dir": managed},
                }
        elif detected == "ue":
            ue = discovery.ue_paths(gd)
            _state.exe = ue.get("exe", "")

        if not _state.exe:
            _state.exe = exe or discovery.pick_main_exe(gd)

        _state.note(f"选定游戏 {gd.name}（引擎 {detected}）")
        out = {"ok": True, **_state.summary()}
        out["detected_engine"] = detected
        return out


# ---------------------------------------------------------------- 引擎嗅探

# IL2CPP global-metadata.dat 的文件头 magic
_IL2CPP_MAGIC = 0xFAB11BAF


def _sniff_engine_for(path: Path) -> str:
    """按文件内容判断该用哪个解析器。

    只在用户没显式指定 engine 时使用。判断依据很硬：
        · global-metadata.dat  → 以 0xFAB11BAF 开头
        · .NET 程序集          → 以 "MZ" 开头（标准 PE）
    判断不出来时**不猜**，返回空串，让上层给出可读的报错。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return ""
    if len(head) >= 4 and int.from_bytes(head, "little") == _IL2CPP_MAGIC:
        return "il2cpp"
    if head[:2] == b"MZ":
        return "mono"
    return ""


# ---------------------------------------------------------------- Mono 程序集挑选
#
# 实现放在 discovery.py（那里也有 mono_paths，两者是配套的）。
# 这里只做转发，避免「哪些 dll 是框架、哪个是游戏代码」这份知识
# 在两处各写一遍，然后慢慢走偏。
_pick_mono_assembly = discovery.pick_mono_assembly


# ---------------------------------------------------------------- 建索引


def ensure_index(
    metadata_path: str = "",
    cache_dir: str = "",
    force_rebuild: bool = False,
    game_dir: str = "",
) -> dict:
    """保证当前游戏有可用的元数据索引（必要时构建）。

    这是所有查询类工具的前置步骤。已建好则直接复用。
    """
    with _state.lock:
        # 允许一次调用里直接把游戏也选好；已经选好同一个游戏则不动。
        if game_dir or metadata_path:
            sel = select_game(game_dir=game_dir, metadata_path=metadata_path)
            if not sel.get("ok"):
                return sel
            if sel.get("reused") and not force_rebuild:
                return {"ok": True, "reused": True, **_state.summary()}

        if _state.has_index() and not force_rebuild:
            return {"ok": True, "reused": True, **_state.summary()}

        if not _state.metadata_path:
            return {
                "ok": False,
                "error": "当前会话没有元数据文件。请先用 select_game 选定游戏，"
                         "或用 metadata_path 指定 global-metadata.dat。",
                **_state.summary(),
            }

        mp = Path(_state.metadata_path)
        if not mp.is_file():
            return {"ok": False, "error": f"元数据文件不存在：{mp}"}

        # 重建前必须先关掉旧连接，否则 Windows 上删不掉被占用的 sqlite 文件
        if force_rebuild and _state.has_index():
            _state.close_index()

        try:
            t0 = time.time()
            idx = Index.open(mp,
                             cache_dir=cache_dir or None,
                             force_rebuild=force_rebuild,
                             engine=_state.engine)
            _state._index = idx
            dt = time.time() - t0
            _state.note(f"索引就绪（{dt:.2f}s）")
            progress(f"索引就绪，耗时 {dt:.2f}s")
            out = {"ok": True, "reused": False, "elapsed": round(dt, 3),
                   **_state.summary()}
            return out
        except MetadataError as exc:
            return {"ok": False, "error": f"元数据解析失败：{exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"构建索引失败：{exc}"}


# ---------------------------------------------------------------- 运行时


def ensure_runtime(
    pid: int = 0,
    auto_find: bool = True,
    spawn: bool = False,
    launcher_exe: str = "",
    preferred_module: str = "",
) -> dict:
    """保证当前游戏有已注入的运行时会话（必要时注入）。

    三种入口：
        pid         指定进程
        launcher_exe 指定可执行文件（会 spawn 并抢在启动前注入）
        auto_find   都不给时，按当前游戏的主程序名自动找进程
    """
    from ..runtime import (attach, spawn_and_attach, SessionError,
                           RuntimeUnavailable, probe_available)

    with _state.lock:
        # ---------------------------------------------------------- 已有会话
        # 这里的判断很关键：早期实现是无条件 `if has_runtime(): return reused`，
        # 于是 inject_process(pid=B) 在已有 pid=A 的会话时**静默返回 A**，
        # launch_game() 也**永远不启动**（直接复用旧会话）。
        # 显式指定的目标被忽略，是比报错更糟的失败——Agent 以为成功了。
        #
        # 现在的规则：
        #   · 没指定目标            → 复用现有会话（这是「懒注入」的正常路径）
        #   · 指定的 pid 就是当前会话 → 复用
        #   · 指定了另一个 pid       → 切换：断开旧的，接上新的
        #   · 要求 spawn 新进程      → 拒绝并说明，绝不静默起第二个游戏进程
        if _state.has_runtime():
            cur = _state.runtime
            cur_pid = getattr(cur, "pid", 0)

            if not pid and not (launcher_exe and spawn):
                # 未指定目标：复用是正确的（Agent 通常只是想「确保有注入」）
                return {"ok": True, "reused": True, **_state.summary()}

            if launcher_exe and spawn:
                # 已经有一个活着的会话，再 spawn 会起**第二个游戏进程**，
                # 两个实例共享存档/配置，行为难以预料。拒绝，并给出出路。
                return {
                    "ok": False,
                    "error": f"已有一个运行中的会话（pid={cur_pid}"
                             f"{'，spawned' if getattr(cur, 'spawned', False) else ''}），"
                             "拒绝再启动新进程。",
                    "hint": "若确实要重新启动，先调用 close_session() 关闭当前会话，"
                            "再调用 launch_game()。"
                            "close_session 只卸载 Hook 并断开连接，不会结束游戏进程。",
                    "current_pid": cur_pid,
                }

            if pid and pid == cur_pid:
                return {"ok": True, "reused": True, **_state.summary()}

            # 指定了另一个 pid：切换过去。
            # close() 只 detach（卸载 Hook、断开连接），**不会杀掉进程**，
            # 所以旧目标会恢复到未被修改的状态并继续运行。
            old_pid = cur_pid
            _state.close_runtime()
            _state.note(f"目标变更（pid {old_pid} → {pid}），"
                        f"已断开旧会话 pid={old_pid}（进程未被结束）")

        # launcher_exe：spawn 模式
        if launcher_exe and spawn:
            try:
                sess = spawn_and_attach(
                    launcher_exe,
                    cwd=str(Path(launcher_exe).parent),
                    preferred_module=preferred_module,
                    # 由本函数统一等待，便于把「等了多久/试了几次」报给 Agent
                    init=False,
                )
            except (SessionError, RuntimeUnavailable) as exc:
                return {"ok": False, "error": str(exc)}
            _state.runtime = sess
            _state.note(f"已启动并注入 {Path(launcher_exe).name} (pid={sess.pid})")
            init = sess.wait_ready(timeout=60.0,
                                   preferred_module=preferred_module)
            if not init.get("ok"):
                # 启动并注入是成功的，只是 IL2CPP 没就绪（可能不是 IL2CPP 游戏）。
                # 如实报告，但仍算成功注入 —— 否则 Agent 会以为注入失败而重试。
                _state.note(f"IL2CPP 初始化未就绪：{init.get('error')}")
                return {
                    "ok": True,
                    "warning": "进程已启动并注入，但 IL2CPP 运行时未就绪",
                    "init": init,
                    **_state.summary(),
                }
            return {"ok": True, "init": init, **_state.summary()}

        # 找 pid
        target_pid = pid
        if not target_pid:
            if not auto_find:
                return {"ok": False, "error": "未指定 pid 且 auto_find 为假"}
            exe_name = Path(_state.exe).name if _state.exe else ""
            cands = discovery.find_processes(only_games=False)
            # 优先按主程序名匹配
            match = None
            if exe_name:
                for p in cands:
                    if p.name.lower() == exe_name.lower():
                        match = p
                        break
            if match is None:
                # 退而求其次：找任意 IL2CPP/Mono 进程
                for p in cands:
                    if p.runtime in ("il2cpp", "mono"):
                        match = p
                        break
            if match is None:
                avail = probe_available()
                return {
                    "ok": False,
                    "error": "没有找到正在运行的目标游戏进程。"
                             "请先启动游戏，或显式传 pid。",
                    "expected_exe": exe_name,
                    "runtime_probe": avail,
                }
            target_pid = match.pid
            proc_name = match.name
        else:
            info = discovery.process_identity(target_pid)
            proc_name = info.get("name", "")

        try:
            sess = attach(target_pid, name=proc_name,
                          preferred_module=preferred_module, init=False)
        except (SessionError, RuntimeUnavailable) as exc:
            return {"ok": False, "error": str(exc), "pid": target_pid}

        _state.runtime = sess
        _state.note(f"已注入 pid={target_pid} ({proc_name})")
        init = sess.wait_ready(timeout=30.0, preferred_module=preferred_module)
        if not init.get("ok"):
            _state.note(f"IL2CPP 初始化未就绪：{init.get('error')}")
            return {
                "ok": True,
                "pid": target_pid,
                "warning": "已注入，但 IL2CPP 运行时未就绪"
                           "（非 IL2CPP 游戏属正常）",
                "init": init,
                **_state.summary(),
            }
        return {"ok": True, "pid": target_pid, "init": init,
                **_state.summary()}


# ---------------------------------------------------------------- 环境探测


def diagnostics(scan_processes: bool = False) -> dict:
    """汇总环境与依赖状态，供 Agent 判断能做什么、不能做什么。

    Args:
        scan_processes: 是否顺带扫本机正在运行的游戏。
            默认 **False** —— 该扫描实测耗时 2.66 秒（枚举 460 个进程），
            而它要回答的问题「有哪些游戏在跑」已经有专门的
            `find_game_processes` 工具。诊断工具不该每次调用都付这笔钱，
            也不该越界去做全机扫描（本 MCP 的运行上下文限定在游戏项目目录内）。
    """
    from ..runtime import probe_available, frida_version

    probes = probe_available()
    info: dict[str, Any] = {
        "python": {
            "version": ".".join(str(x) for x in __import__("sys").version_info[:3]),
            "executable": __import__("sys").executable,
        },
        "frida": {
            "installed": probes.get("available", False),
            "version": frida_version(),
            "detail": probes,
        },
        "engines": {
            "il2cpp": {
                "parser": "内置（纯标准库）",
                "ready": True,
                "note": "解析 global-metadata.dat，已在真实游戏上验证",
            },
            "mono": {
                "parser": "内置 ECMA-335 解析器",
                "ready": _mono_ready(),
                "note": "解析 Unity Mono 后端的 .NET 程序集",
            },
            "ue": {
                "parser": "内置（基于真实样本实测能力）",
                "ready": _ue_ready(),
                "note": "Unreal 的强项是资源容器解析，不是类型系统；"
                        "字段偏移/FName 池在静态样本中不可得",
                "capabilities": UE_CAPABILITIES,
            },
        },
        "cache_dir": str(default_cache_dir_safe()),
        "session": _state.summary(),
    }

    # 反编译是**可选**能力（外部 ilspycmd，不是运行期依赖）。
    # 必须显式报告可用性：缺了它 Agent 应改走元数据索引，而不是反复试错。
    try:
        from . import decompile
        info["decompiler"] = decompile.available()
    except Exception as exc:
        info["decompiler"] = {
            "available": False, "tool": "", "version": "",
            "hint": f"反编译模块加载失败：{exc}",
        }

    # 探测本机可用的游戏 —— 仅在显式要求时做（默认关闭，见 docstring）
    if scan_processes:
        try:
            all_procs = discovery.find_processes(only_games=True)
            info["running_games"] = [
                {"pid": p.pid, "name": p.name, "runtime": p.runtime}
                for p in all_procs[:20]
            ]
            info["running_game_count"] = len(all_procs)
        except Exception as exc:
            info["running_games"] = []
            info["running_games_error"] = str(exc)
    else:
        info["running_games_skipped"] = (
            "默认不扫本机进程（耗时约 2.7s）；要列运行中的游戏请用 "
            "find_game_processes，或传 scan_processes=True"
        )

    # 顺带报一下 PE 解析能力是否可用
    try:
        import importlib.util
        info["optional_packages"] = {
            name: importlib.util.find_spec(name) is not None
            for name in ("capstone", "pefile", "frida", "mcp")
        }
    except Exception:
        info["optional_packages"] = {}

    return info


def default_cache_dir_safe() -> str:
    try:
        from .index import default_cache_dir
        return str(default_cache_dir())
    except Exception:
        return ""


def _mono_ready() -> bool:
    try:
        from . import mono_meta  # noqa: F401
        return True
    except Exception:
        return False


def _ue_ready() -> bool:
    try:
        from . import ue  # noqa: F401
        return True
    except Exception:
        return False


# UE 的「能做什么」与 IL2CPP/Mono 差别很大，不能用一个布尔值概括：
# 资源容器解析已实测可用，但类型系统（FName 池、属性偏移、.usmap）
# 在静态样本里拿不到。把边界写清楚，免得 Agent 以为 UE 也能查字段偏移。
UE_CAPABILITIES = {
    "detect_game": "已实现：靠目录布局、打包清单、/Script/ 字符串识别",
    "pak_index": "已实现：.pak v11/v12 文件清单（实测与打包清单 100% 一致）",
    "iostore_index": "已实现：.utoc v8 目录索引（含 UTF-16 中文路径）",
    "manifest": "已实现：Manifest_*Files*.txt 解析",
    "script_objects": "已实现：exe 内 /Script/ 模块与对象路径（字符串证据）",
    "usmap": "未实现：样本中无 .usmap 文件，无可依据的真实格式",
    "fname_pool": "未实现：样本中无法定位 FName 池",
    "property_offsets": "未实现：需运行时读取，静态样本拿不到",
    "ue_version": "未实现：样本中无可信版本来源，返回空值而非猜测",
}


# ---------------------------------------------------------------- 静态分析辅助


def inspect_binary(path: str) -> dict:
    """分析一个可执行文件/DLL 的 PE 结构（不启动它）。"""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": f"文件不存在：{path}"}
    try:
        # 只解析一次。早先这里是 `info = pe_parse(p)` + `pe_summarize(p)`，
        # 而 summarize 内部又会 parse 一遍（pe.py:484）—— 于是同一个文件
        # 被完整解析两次，且 `info` **从未被使用**（实测白付 0.55 ms，
        # 约占该工具耗时的一半）。
        #
        # 去掉 `pe_parse` 不改变语义：文件非法时两者抛的都是同一个
        # PEError（summarize 走的就是 parse），下面照旧捕获。
        out = pe_summarize(p)
        return {"ok": True, "path": str(p), "pe": out}
    except Exception as exc:
        return {"ok": False, "error": f"PE 解析失败：{exc}", "path": str(p)}
