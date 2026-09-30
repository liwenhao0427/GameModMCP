"""游戏发现：进程枚举与引擎自动识别。

不依赖 psutil —— 用 ctypes 直接调 Win32 API，保证零第三方依赖。

注：原先还有一套「扫 Steam 库列出本机装了哪些游戏」的逻辑，
随 detect_games 工具一并移除了 —— 本 MCP 每次运行都限定在
一个游戏项目目录内，全机扫描属于噪声能力。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import enum
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..logutil import warn
from . import pe

# ---------------------------------------------------------------- Mono 程序集

# 游戏自己的代码几乎总在这几个程序集里；按优先级排（越靠前越可能是游戏逻辑）
MONO_PREFERRED_ASSEMBLIES = (
    "Assembly-CSharp.dll",
    "Assembly-CSharp-firstpass.dll",
    "Assembly-CSharp-Editor.dll",
)

# Unity / .NET 自带的框架程序集前缀，不是游戏逻辑
MONO_FRAMEWORK_PREFIXES = (
    "UnityEngine", "Unity.", "System", "mscorlib", "netstandard",
    "Mono.", "Microsoft.", "Newtonsoft", "nunit", "ExCSS", "I18N",
)

# ---------------------------------------------------------------- Win32 常量

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
MAX_PATH = 260
TH32CS_SNAPPROCESS = 0x00000002
LIST_MODULES_ALL = 0x03

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 不属于游戏、但常驻的系统/工具进程（用于过滤噪音）
NOISE_NAMES = {
    "svchost", "conhost", "idle", "system", "runtimebroker", "dwm", "csrss",
    "winlogon", "searchhost", "textinputhost", "shellexperiencehost",
    "startmenuexperiencehost", "webview2", "msedge", "chrome", "chromium",
    "explorer", "tasklist", "python", "python3", "pythonw", "node", "pwsh",
    "powershell", "cmd", "conhost", "crashpad_handler", "steam", "steamwebhelper",
    "aimodv1", "devenv", "code", "gameoverlayui", "audiodg", "spoolsv",
    "searchindexer", "securityhealthservice", "widgets", "sihost", "ctfmon",
    "taskhostw", "dllhost", "smartscreen", "applicationframehost",
    "backgroundtaskhost", "lockapp", "fontdrvhost", "lsass", "services",
    "wininit", "wmiprvse", "nvcontainer", "nvidia web helper", "igfxem",
    "razer synapse", "armourycrate", "msiafterburner", "rtss",
}

# 明显是游戏运行时的模块名
RUNTIME_MODULES = {
    "gameassembly.dll": "il2cpp",
    "mono-2.0-bdwgc.dll": "mono",
    "mono-2.0-sgen.dll": "mono",
    "mono.dll": "mono",
}


# ---------------------------------------------------------------- 数据结构


@dataclass
class ProcessInfo:
    """一个候选游戏进程。"""

    pid: int
    name: str
    exe_path: str = ""
    window_title: str = ""
    runtime: str = "unknown"
    is_64: bool | None = None
    architectures: list[str] = field(default_factory=list)

    @property
    def image_name(self) -> str:
        return Path(self.exe_path).name if self.exe_path else self.name


# ---------------------------------------------------------------- 进程枚举


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def process_exe_path(pid: int) -> str:
    """取进程完整映像路径（需要 PROCESS_QUERY_LIMITED_INFORMATION）。"""
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(handle)


def process_identity(pid: int) -> dict:
    """取进程身份三元组：pid + path + 创建时间。

    用于检测「进程已重启但 PID 复用」的情况 —— AiMod 也用了同样的思路。
    """
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return {"pid": pid, "path": "", "created_at": 0, "valid": False}

    try:
        path = ""
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            path = buf.value

        created = 0
        creation, exit_code, kernel, user = (wt.FILETIME() for _ in range(4))
        if k32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_code),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime

        return {"pid": pid, "path": path, "created_at": created, "valid": bool(path)}
    finally:
        k32.CloseHandle(handle)


def enumerate_processes() -> list[tuple[int, str]]:
    """枚举所有进程，返回 (pid, 映像名) 列表。"""
    k32 = _kernel32()
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []

    out: list[tuple[int, str]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            out.append((entry.th32ProcessID, entry.szExeFile))
            if not k32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snapshot)

    return out


def process_arch(pid: int) -> tuple[bool, list[str]]:
    """判定进程位数与已加载的架构标记模块。

    返回 (is_64, modules_matched)。
    依据：Wow64 判定 + 是否加载了 64 位运行时模块。
    """
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    is_64: bool | None = None
    if handle:
        try:
            wow64 = wt.BOOL()
            if k32.IsWow64Process(handle, ctypes.byref(wow64)):
                is_64 = not bool(wow64.value)
        finally:
            k32.CloseHandle(handle)

    if is_64 is None:
        return False, []

    return is_64, []


def list_modules(pid: int, limit: int = 0) -> list[str]:
    """列出进程已加载的模块名（小写）。"""
    k32 = _kernel32()
    # 用 TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32 需要目标架构匹配；
    # 这里用 EnumProcessModules 更稳。psapi 是系统 DLL，始终可用。
    PROCESS_VM_READ_ = 0x0010
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ_, False, pid)
    if not handle:
        return []

    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        needed = wt.DWORD(0)
        if not psapi.EnumProcessModulesEx(
            handle, None, 0, ctypes.byref(needed), LIST_MODULES_ALL
        ):
            return []

        count = needed.value // ctypes.sizeof(ctypes.c_void_p)
        if count <= 0 or count > 4096:
            return []

        arr = (ctypes.c_void_p * count)()
        if not psapi.EnumProcessModulesEx(
            handle, arr, needed.value, ctypes.byref(needed), LIST_MODULES_ALL
        ):
            return []

        out: list[str] = []
        buf = ctypes.create_unicode_buffer(MAX_PATH)
        for h in arr:
            if not h:
                continue
            if psapi.GetModuleBaseNameW(handle, ctypes.c_void_p(h), buf, MAX_PATH):
                out.append(buf.value.lower())
            if limit and len(out) >= limit:
                break
        return out
    finally:
        k32.CloseHandle(handle)


def detect_runtime(pid: int) -> str:
    """检测目标进程使用的脚本运行时。"""
    mods = set(list_modules(pid))
    if not mods:
        return "unknown"

    for mod, rt in RUNTIME_MODULES.items():
        if mod in mods:
            return rt

    # UE 的特征模块
    for m in mods:
        if m.startswith("ue4") or m.startswith("ue5") or m in (
            "unrealengine.dll", "ue4game.exe", "ue4-editor.exe",
        ):
            return "ue"

    return "unknown"


def is_game_process(pid: int) -> bool:
    """启发式判断某进程是否像游戏。

    判据（满足任一即可）：
    - 加载了 il2cpp / mono / UE 运行时
    - 有可见窗口且不在噪音名单里
    """
    if detect_runtime(pid) != "unknown":
        return True

    exe = process_exe_path(pid)
    if not exe:
        return False

    stem = Path(exe).stem.lower()
    if stem in NOISE_NAMES:
        return False

    title = window_title_for_pid(pid)
    return bool(title)


def window_title_for_pid(pid: int) -> str:
    """取某 PID 的主窗口标题（用于辅助识别游戏）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[str] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def callback(hwnd, _lparam):
        wnd_pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
        if wnd_pid.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value:
                    found.append(buf.value)
                    return False  # 找到一个就够了
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(callback), 0)
    except Exception:  # noqa: BLE001 - 窗口枚举失败不影响主流程
        return ""
    return found[0] if found else ""


def find_processes(name_filter: str = "", only_games: bool = True) -> list[ProcessInfo]:
    """查找进程。

    name_filter: 子串匹配（大小写不敏感）；空表示全部。
    only_games : 是否只保留像游戏的进程。
    """
    needle = name_filter.lower().strip()
    out: list[ProcessInfo] = []

    for pid, name in enumerate_processes():
        if pid <= 4:
            continue
        if needle and needle not in name.lower():
            continue

        stem = Path(name).stem.lower()
        if only_games and stem in NOISE_NAMES:
            continue

        exe = process_exe_path(pid)
        if only_games and not exe:
            continue

        runtime = detect_runtime(pid)
        if only_games and runtime == "unknown":
            # 没有已知运行时，必须有可见窗口才认为是游戏
            title = window_title_for_pid(pid)
            if not title:
                continue
        else:
            title = window_title_for_pid(pid)

        is_64, _ = process_arch(pid)
        out.append(
            ProcessInfo(
                pid=pid,
                name=name,
                exe_path=exe,
                window_title=title,
                runtime=runtime,
                is_64=is_64,
            )
        )

    # 有运行时的排前面，其次按名字
    out.sort(key=lambda p: (p.runtime == "unknown", p.name.lower()))
    return out


# ---------------------------------------------------------------- 引擎探测


def pick_main_exe(game_dir: str | Path) -> str:
    """在游戏目录里挑出主程序 exe。

    优先级：与目录同名的 exe > 最大的非辅助 exe
    """
    game_dir = Path(game_dir)
    if not game_dir.is_dir():
        return ""

    skip = {
        "crashpad_handler", "unitycrashhandler64", "unitycrashhandler32",
        "unins000", "vcredist_x64", "vcredist_x86", "dxsetup", "setup",
        "launcher_installer", "python", "node",
    }

    candidates: list[tuple[int, int, str]] = []
    for exe in list(game_dir.glob("*.exe")) + list(game_dir.glob("*/*.exe")):
        stem = exe.stem.lower()
        if stem in skip or stem.endswith("crashhandler") or stem.endswith("crashhandler64"):
            continue
        try:
            size = exe.stat().st_size
        except OSError:
            continue
        # 与目录同名者优先（0），其次按大小降序
        same_name = 0 if stem == game_dir.name.lower() else 1
        candidates.append((same_name, -size, str(exe)))

    if not candidates:
        return ""
    candidates.sort()
    return candidates[0][2]


def il2cpp_paths(game_dir: str | Path) -> dict:
    """定位 IL2CPP 的关键文件。

    返回 {"game_assembly":..., "metadata":..., "unity_player":..., "data_dir":...}
    缺失项为 ""。
    """
    game_dir = Path(game_dir)
    result = {"game_assembly": "", "metadata": "", "data_dir": ""}

    # GameAssembly.dll 通常在游戏根目录（与 exe 同级）
    for candidate in (
        game_dir / "GameAssembly.dll",
        game_dir / "GameAssembly.dll".lower(),
    ):
        if candidate.is_file():
            result["game_assembly"] = str(candidate)
            break

    # *_Data 目录：可能是 <exe名>_Data 或 UnityPlayer 相关
    data_dirs = [d for d in game_dir.glob("*_Data") if d.is_dir()]
    if not data_dirs:
        # 有时 Data 目录在子目录里
        data_dirs = [d for d in game_dir.glob("*/*_Data") if d.is_dir()]

    for data_dir in data_dirs:
        meta = data_dir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        if meta.is_file():
            result["metadata"] = str(meta)
            result["data_dir"] = str(data_dir)
            break

    if not result["metadata"]:
        # 全局兜底搜索
        for meta in game_dir.rglob("global-metadata.dat"):
            if meta.is_file():
                result["metadata"] = str(meta)
                result["data_dir"] = str(meta.parent.parent.parent)
                break

    return result


def mono_paths(game_dir: str | Path) -> dict:
    """定位 Mono 的程序集目录与程序集列表。"""
    game_dir = Path(game_dir)
    result = {"managed_dir": "", "assemblies": []}

    for data_dir in list(game_dir.glob("*_Data")) + list(game_dir.glob("*/*_Data")):
        managed = data_dir / "Managed"
        if managed.is_dir():
            result["managed_dir"] = str(managed)
            # 不截断：glob 顺序不确定，截断可能正好丢掉 Assembly-CSharp.dll。
            # 目录里通常也就一两百个 dll，全收下没有代价。
            result["assemblies"] = sorted(str(p) for p in managed.glob("*.dll"))
            # 顺便找一下 Mono 运行时本体（有它才说明是 Mono 后端）
            for rt in ("mono-2.0-bdwgc.dll", "mono-2.0-sgen.dll", "mono.dll"):
                if (data_dir.parent / rt).is_file() or (game_dir / rt).is_file():
                    result["runtime"] = rt
                    break
            break

    return result


def pick_mono_assembly(assemblies: list[str]) -> str:
    """从托管程序集列表里挑出「游戏自己的代码」那个 dll。

    为什么要专门挑：一个 Unity Mono 游戏的 Managed 目录里通常有
    100~200 个 dll，其中绝大多数是 UnityEngine/System 等框架程序集。
    **游戏逻辑几乎总在 Assembly-CSharp.dll 里**，直接指出它，
    Agent 就不用在一个目录里猜哪个才是目标。

    挑选优先级：
        1. 精确命中 Assembly-CSharp.dll 之类的首选名
        2. 不在框架程序集前缀里的、体积最大的那个
    挑不出来返回空串（由调用方报错），不要瞎猜一个框架 dll 当游戏代码。
    """
    if not assemblies:
        return ""
    by_name = {os.path.basename(p).lower(): p for p in assemblies}
    for want in MONO_PREFERRED_ASSEMBLIES:
        hit = by_name.get(want.lower())
        if hit:
            return hit

    candidates = []
    for p in assemblies:
        base = os.path.basename(p)
        if base.startswith(MONO_FRAMEWORK_PREFIXES):
            continue
        try:
            candidates.append((os.path.getsize(p), p))
        except OSError:
            continue
    if not candidates:
        return ""
    # 体积最大的通常就是游戏主逻辑（Assembly-CSharp 一般几百 KB ~ 几 MB）
    return max(candidates)[1]


def ue_paths(game_dir: str | Path) -> dict:
    """定位 Unreal Engine 的关键目录。"""
    game_dir = Path(game_dir)
    result = {"paks": "", "exe": "", "engine_dir": ""}

    # ProjectName/Binaries/Win64/ProjectName-Win64-Shipping.exe
    for binaries in game_dir.glob("*/Binaries/Win64"):
        for exe in binaries.glob("*-Win64-Shipping.exe"):
            result["exe"] = str(exe)
            break
        if result["exe"]:
            break

    for content in game_dir.glob("*/Content/Paks"):
        if content.is_dir():
            result["paks"] = str(content)
            break

    for engine in game_dir.glob("*/Binaries/Win64/Engine"):
        result["engine_dir"] = str(engine)
        break

    return result


def detect_engine(game_dir: str | Path) -> str:
    """自动识别游戏引擎。返回 il2cpp / mono / ue / unknown。"""
    game_dir = Path(game_dir)
    if not game_dir.is_dir():
        return "unknown"

    if il2cpp_paths(game_dir)["metadata"]:
        return "il2cpp"

    if mono_paths(game_dir)["managed_dir"]:
        return "mono"

    # UE 特征：*.pak 与 shipped exe
    if list(game_dir.glob("*/Content/Paks/*.pak")) or list(
        game_dir.glob("*/Binaries/Win64/*-Win64-Shipping.exe")
    ):
        return "ue"

    if (game_dir / "Engine").is_dir():
        return "ue"

    # 纯 Unity（无脚本后端信息）
    if list(game_dir.glob("*_Data")) and not il2cpp_paths(game_dir)["metadata"]:
        return "unity"

    return "unknown"


def describe_game(game_dir: str | Path) -> dict:
    """给出游戏目录的完整探测报告。"""
    game_dir = Path(game_dir)
    engine = detect_engine(game_dir)
    info: dict = {
        "install_dir": str(game_dir),
        "exists": game_dir.is_dir(),
        "engine": engine,
        "main_exe": pick_main_exe(game_dir) if game_dir.is_dir() else "",
    }

    if engine == "il2cpp":
        info["il2cpp"] = {
            k: v for k, v in il2cpp_paths(game_dir).items() if k != "data_dir"
        }
    elif engine == "mono":
        mono = mono_paths(game_dir)
        picked = pick_mono_assembly(mono["assemblies"])
        info["mono"] = {
            "managed_dir": mono["managed_dir"],
            "assembly_count": len(mono["assemblies"]),
            # 指出「哪一个 dll 是游戏自己的代码」——这是 open_game 会索引的那个，
            # Agent 拿到它就能直接读/反编译，不用在 160 个 dll 里猜。
            "game_assembly": picked,
            "game_assembly_name": Path(picked).name if picked else "",
        }
    elif engine == "ue":
        info["ue"] = ue_paths(game_dir)

    # 顺带报一下主程序的运行时
    exe = info.get("main_exe")
    if exe and Path(exe).is_file():
        try:
            pinfo = pe.summarize(exe)
            info["main_exe_runtime"] = pinfo["runtime"]
            info["main_exe_is_64"] = pinfo["is_64"]
        except Exception as exc:  # noqa: BLE001 - 探测失败不影响整体报告
            warn(f"解析主程序失败: {exc}")

    return info
