#!/usr/bin/env python3
"""测试样本与路径的**集中定义**。所有 verify_*.py 都必须从这里取。

为什么要集中
============================================================
历史上 11 个验证脚本各自硬编码了工作区绝对路径：

    sys.path.insert(0, r"C:\\Users\\Administrator\\Documents\\deepseek-harness\\默认工作区\\GameModMCP\\src")

工作区一挪，整套测试就**全部跑不起来**，而脚本不会报「路径错了」，
只会 ModuleNotFoundError 或更糟——静默用了错误的样本。
绝对路径是本项目最容易腐坏的东西，所以收敛到这一个文件。

约定
============================================================
- 仓库根 / src 路径：由本文件位置**推导**，永不硬编码。
- 样本游戏：只记录「可能的位置」，用 `find_*` 拿；取不到返回 None。
- 样本不存在时调用方必须记为「无法验证」并跳过，**不许假装通过**。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 中文 Windows 的控制台默认 GBK，本模块直接运行时（`python devtools\samples.py`）
# 会打印 ✅ / ⚠️，不做这一步会抛 UnicodeEncodeError。
# 注意：本模块是被各 verify_*.py **import** 的，所以这里只影响它自己的
# 直接运行；被 import 时上层脚本已各自设好编码。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- 仓库自身路径

#: 仓库根（devtools/ 的上一级）
ROOT: Path = Path(__file__).resolve().parent.parent
#: 源码根，验证脚本用它设 sys.path
SRC: Path = ROOT / "src"
#: 测试缓存目录（已被 .gitignore 排除）
CACHE_TEST: Path = ROOT / ".cache-test"
#: 跨引擎测试缓存（已被 .gitignore 排除）
CACHE_ADAPT_TEST: Path = ROOT / ".cache-adapt-test"


def add_src_to_path() -> None:
    """把仓库的 src 加进 sys.path，供验证脚本 import gamemod_mcp。

    用本文件位置推导，因此换机器、换目录都不需要改。
    """
    import sys
    p = str(SRC)
    if p not in sys.path:
        sys.path.insert(0, p)


def ensure_cache_dirs() -> None:
    """确保测试缓存目录存在。"""
    CACHE_TEST.mkdir(parents=True, exist_ok=True)
    CACHE_ADAPT_TEST.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 样本根目录

#: 样本游戏可能所在的根。用环境变量 GAMEMOD_SAMPLES 可整体重定向。
SAMPLE_ROOTS: list[Path] = [
    Path(os.environ["GAMEMOD_SAMPLES"]) if os.environ.get("GAMEMOD_SAMPLES") else None,
    Path(r"D:\Projects"),
    Path.home() / "Projects",
]
SAMPLE_ROOTS = [p for p in SAMPLE_ROOTS if p is not None]


def _find_dir(names: list[str], marker: str = "") -> Path | None:
    """在样本根下找一个目录。

    Args:
        names:  候选目录名（按优先级）
        marker: 必须存在的相对路径，用于确认目录内容正确（不是同名的空壳）
    """
    for root in SAMPLE_ROOTS:
        for n in names:
            cand = root / n
            if not cand.is_dir():
                continue
            if marker and not (cand / marker).exists():
                continue
            return cand
    return None


# ---------------------------------------------------------------- 各引擎样本

def find_il2cpp() -> Path | None:
    """IL2CPP 样本：Garden of Witches（元数据实测约 17 MB）。"""
    return _find_dir(["Garden of Witches"],
                     marker=r"Garden of Witches_Data/il2cpp_data/Metadata/global-metadata.dat")


def find_mono() -> Path | None:
    """Mono 样本：Archon Soul（单程序集）。"""
    return _find_dir(["Archon Soul"],
                     marker=r"Archon Soul_Data/Managed/Assembly-CSharp.dll")


def find_mono_multi() -> Path | None:
    """Mono 多程序集样本：Kinny and the Cosmic Cauldron。"""
    return _find_dir(["Kinny and the Cosmic Cauldron"],
                     marker=r"Kinny and the Cosmic Cauldron_Data/Managed/Assembly-CSharp.dll")


def find_ue_pak_v12() -> Path | None:
    """UE 样本：CatapultHero（pak v12 / utoc v8）。"""
    return _find_dir(["CatapultHero"], marker="Manifest_UFSFiles_Win64.txt")


def find_ue_pak_v11() -> Path | None:
    """UE 样本：Dinoblade（pak v11 / utoc v8）。"""
    return _find_dir(["Dinoblade"], marker="Manifest_UFSFiles_Win64.txt")


def find_il2cpp_dump() -> Path | None:
    """IL2CPP 字段类型真值：游戏官方 dump（含 /*0x20*/ 偏移注释）。

    这是 IL2CPP 字段类型的**最高可信度真值**——它是游戏/dumper 自己生成的，
    不是我们自己写的第二个解析器。
    """
    game = find_il2cpp()
    if game is None:
        return None
    # 常见位置：aiMod/dump/<exe 名>/il2cpp_dump.cs
    for root in SAMPLE_ROOTS:
        cand = root / "aiMod" / "dump" / f"{game.name}.exe" / "il2cpp_dump.cs"
        if cand.is_file():
            return cand
    # 退化：在 aiMod/dump 下找任意 il2cpp_dump.cs
    for root in SAMPLE_ROOTS:
        base = root / "aiMod" / "dump"
        if base.is_dir():
            hits = list(base.rglob("il2cpp_dump.cs"))
            if hits:
                return hits[0]
    return None


def find_mono_groundtruth() -> Path | None:
    """Mono 真值生成器（微软官方 System.Reflection.Metadata）。"""
    d = ROOT / "devtools" / "groundtruth" / "mono"
    return d if d.is_dir() else None


# ---------------------------------------------------------------- 便捷访问器

def game_exe(game_dir: Path) -> Path | None:
    """在一个游戏目录里找出主程序 exe。"""
    if game_dir is None:
        return None
    exes = sorted(game_dir.glob("*.exe"))
    return exes[0] if exes else None


def require(path: Path | None, what: str) -> bool:
    """报告一个样本是否可用，不可用时打印「无法验证」而不是失败。"""
    if path is None:
        print(f"  ⚠️  无法验证：样本「{what}」不在本机")
        print(f"      可用环境变量 GAMEMOD_SAMPLES 指定样本根目录")
        print(f"      当前查找位置：{[str(p) for p in SAMPLE_ROOTS]}")
        return False
    return True


if __name__ == "__main__":
    # 直接运行本文件 = 打印本机样本可用性，方便排查
    print("测试样本可用性")
    print("=" * 66)
    print(f"仓库根      : {ROOT}")
    print(f"源码根      : {SRC}  （存在={SRC.is_dir()}）")
    print(f"测试缓存    : {CACHE_TEST}")
    print(f"样本根      : {[str(p) for p in SAMPLE_ROOTS]}")
    print("-" * 66)
    for label, fn in [
        ("IL2CPP (Garden of Witches)", find_il2cpp),
        ("Mono (Archon Soul)", find_mono),
        ("Mono 多程序集 (Kinny)", find_mono_multi),
        ("UE pak v12 (CatapultHero)", find_ue_pak_v12),
        ("UE pak v11 (Dinoblade)", find_ue_pak_v11),
        ("IL2CPP 字段真值 (il2cpp_dump.cs)", find_il2cpp_dump),
        ("Mono 真值生成器", find_mono_groundtruth),
    ]:
        p = fn()
        mark = "✅" if p is not None else "⚠️ "
        size = ""
        if p is not None and p.is_file():
            size = f"  ({p.stat().st_size / 1024 / 1024:.1f} MB)"
        print(f"  {mark} {label:<36} {p if p else '不可用'}{size}")
