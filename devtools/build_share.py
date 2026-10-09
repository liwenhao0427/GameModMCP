#!/usr/bin/env python3
"""把 GameMod MCP 打成一个**自带依赖、开箱即用**的分享包。

为什么要一个脚本而不是手工拷目录
============================================================
手工拷必然漂移：漏一个文件就能让对方的 AI 装不起来，而我们这边全绿
（这正是本项目反复踩的坑 —— 「仓库修好了」不等于「对方能用」）。
所以打包这件事**必须可复现**：一条命令生成，且打包后**自检**。

产物形态（`E:\\project\\GameModMCP-share\\`，仓库同级，不进 git）
============================================================
    GameModMCP-share/
    ├── 安装说明.md            ← 给 AI 读的入口（**最关键的一个文件**）
    ├── install.py             ← 一键安装（**主推**：纯 Python，无编码问题）
    ├── install.ps1            ← 备选（只在用户已有 pwsh 7 时可用）
    ├── README.md              ← 完整说明（从仓库拷，含生成好的工具表）
    ├── requirements.txt
    ├── src/                   ← MCP 服务器源码（含 agent.js 探针）
    ├── skills/                ← skill 说明（gamemod://guide 资源要读它）
    ├── mcp-config/            ← 各客户端现成配置
    ├── devtools/              ← 验收脚本（对方可自证「装对了」）
    ├── docs/                  ← 设计与评估文档
    └── wheels/                ← 依赖 wheel（离线可装）
        ├── required/          ← mcp + frida（必需，~63 MB）
        └── optional/          ← 截图 / OCR（可选，~95 MB）

平台：**只支持 Windows**（用户明确要求）。所以不带 `install.sh`，
wheel 也只下 Windows 的。

为什么主推 `install.py` 而不是 `.ps1`
============================================================
Windows 自带的 **PowerShell 5.1**（`powershell.exe`）读**无 BOM 的 UTF-8**
脚本时按系统 ANSI（中文机器上是 GBK）解码 → 中文变乱码 → **语法报错**。
实测：

    powershell -File install.ps1
    → The string is missing the terminator: ".

而 `pwsh`（PowerShell 7）**不一定装了**。对方装了 Python 才能跑 MCP，
所以 **Python 一定在** —— 用它最稳。`install.ps1` 保留给已有 pwsh 7 的用户。

为什么把 wheel 内置
============================================================
对方环境不可控：可能没有外网、pip 源不稳、公司内网需要认证。
内置 wheel 后 `install.py` 能**完全离线**装好必需依赖；
可选依赖一并内置，但装不装由对方决定（不装不影响其它 40+ 个工具）。

用法
============================================================
    python devtools/build_share.py              # 打包（会下载 wheel）
    python devtools/build_share.py --no-wheels  # 只打源码（体积小）
    python devtools/build_share.py --check      # 只校验已有分享包是否完整
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# ⚠️ 必须设 stdout 编码：本脚本会打印 [OK]/[FAIL]/⚠️ 这类非 GBK 字符，
# 而中文 Windows 控制台默认 cp936，直接 print 会抛 UnicodeEncodeError
# —— **结论拿不到**（本项目踩过多次，`verify_encoding_console.py` 就是
# 为拦这个而存在的门禁；它抓到过本脚本漏了这一行）。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
#: 分享包落在**仓库同级**：既不进 git，也不会被仓库的文档门禁递归扫到
#: （`verify_tool_inventory.py` 会扫全仓 `*.md`，包放仓内会连带扫到）。
SHARE = ROOT.parent / "GameModMCP-share"

#: 必需依赖。
#: ⚠️ mcp **必须 <2** —— 打包时实测到的真实缺陷：本代码用 1.x 的
#: `mcp.server.fastmcp.FastMCP`，而 2.x 已改名为 MCPServer
#: （`mcp.server.mcpserver`）。只写 `>=1.0.0` 会让干净环境装到 2.x
#: 然后服务器**起不来**（本机因早已装了 1.26.0 而从不暴露）。
REQUIRED = ["mcp>=1.0.0,<2", "frida>=16.0.0"]
#: 可选（弱视觉：截图 + OCR）。缺了不影响其它工具。
OPTIONAL = [
    "dxcam",              # 截图首选（对全屏独占更稳）
    "mss",                # 截图回退
    "rapidocr-onnxruntime",   # OCR 首选（中文好）
    "onnxruntime",        # rapidocr 的推理后端
    "numpy",
    "opencv-python",      # 缩放/存 PNG/合成图
    "pillow",
    "pywin32",            # 按窗口标题定位
]

#: 要拷进包的目录（源 → 包内相对路径）
COPY_DIRS = ["src", "skills", "mcp-config", "devtools", "docs"]
#: 要拷进包的单文件（**不带 install.sh** —— 本项目只支持 Windows）
COPY_FILES = ["README.md", "install.py", "install.ps1", "requirements.txt",
              "AGENTS.md"]

#: 不拷进包的（体积/无意义/本机产物）
SKIP_NAMES = {"__pycache__", ".git", ".cache", ".cache-test",
              ".cache-adapt-test", ".cache-lazy-test", ".cache-matrix",
              ".cache-verify", "groundtruth", "archive", "agent_logs"}


def log(msg: str) -> None:
    print(msg, flush=True)


def count_pkg_tools(pkg: Path, timeout: int = 180) -> int:
    """用**包内的 src** 起一次服务器，数注册了多少工具。

    这是整条链路上**最强**的一条验证：它真的 import 了包里的源码，
    所以能抓到「拷漏了文件」「依赖装错版本」这类只有对方才会踩到的问题。
    实测就是靠它发现 `mcp>=1.0.0` 会装到 2.x 导致服务器起不来。

    拿不到时返回 0（调用方按失败处理）。
    """
    src = pkg / "src"
    if not src.is_dir():
        return 0
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src)
    env["PYTHONUTF8"] = "1"
    # 禁写字节码：否则这次自检会在包内生成 __pycache__，
    # 让「不含 __pycache__」那条断言报红（验证动作改了被验证对象）。
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = ("import asyncio,sys;"
            "sys.path.insert(0,r'%s');"
            "from gamemod_mcp import server;"
            "print('TOOLCOUNT=',len(asyncio.run(server.mcp.list_tools())))"
            % str(src))
    try:
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=timeout)
    except Exception:
        return 0
    for ln in ((r.stdout or "") + (r.stderr or "")).splitlines():
        if "TOOLCOUNT=" in ln:
            try:
                return int(ln.split("TOOLCOUNT=")[1].strip())
            except Exception:
                return 0
    return 0


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


# ---------------------------------------------------------------- 拷贝

def copy_tree(src: Path, dst: Path) -> int:
    """拷目录，跳过 SKIP_NAMES 与常见产物。返回文件数。

    ⚠️ 先判断**自己**是不是在被跳过的目录里再递归 —— `rglob` 会先枚举
    再过滤，`__pycache__` 下的 `.pyc` 虽然被后缀规则挡掉，但目录本身
    会被 mkdir 出来（实测漏进包里过）。所以这里用 os.walk 显式剪枝。
    """
    n = 0
    for dirpath, dirnames, filenames in os.walk(src):
        # 原地改 dirnames 即可剪枝，walk 不会进这些目录
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
        cur = Path(dirpath)
        for fn in filenames:
            if Path(fn).suffix in (".pyc", ".pyo"):
                continue
            rel = (cur / fn).relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cur / fn, target)
            n += 1
    return n


def prune_artifacts(pkg: Path) -> list[str]:
    """删掉包里不该有的东西（字节码目录等）。返回删掉的路径。"""
    removed: list[str] = []
    for p in list(pkg.rglob("__pycache__")):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed.append(str(p.relative_to(pkg)))
    for p in list(pkg.rglob("*.pyc")):
        p.unlink(missing_ok=True)
        removed.append(str(p.relative_to(pkg)))
    return removed


def download_wheels(specs: list[str], out: Path) -> tuple[bool, str]:
    """把依赖的 wheel 下到 out（含依赖树）。返回 (成功, 说明)。"""
    out.mkdir(parents=True, exist_ok=True)
    # --only-binary=:all: 保证拿到 wheel 而不是源码包（源码包对方可能编不出来）
    cmd = [sys.executable, "-m", "pip", "download", *specs,
           "-d", str(out), "--only-binary=:all:"]
    r = run(cmd, timeout=1800)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "")[-500:]
    return True, ""


# ---------------------------------------------------------------- 入口文件

INSTALL_DOC = """# GameMod MCP —— 安装说明（给 AI 读）

> 这是一份**可直接执行的安装说明**。请严格按顺序做完，并如实报告每步结果。
> 目标：把这台机器上的 GameMod MCP 装好，让用户的 AI 客户端能连上它。

## 这个包是什么

**GameMod MCP** 是一个游戏 Mod 开发工具集（MCP 服务器），{tool_hint} 个工具，支持：

| 引擎 | 能做什么 |
|---|---|
| **IL2CPP**（Unity 最常见） | 查类/字段/方法、取运行时真实偏移、Hook 函数、读内存、枚举活对象实例 |
| **Mono**（Unity 老版 / 开发版） | 同上，外加**直接反编译出 C# 源码** |
| **Unreal** | 看 pak/utoc 里打包了哪些资源、读打包清单 |

还能自动验收 Mod：截图 + OCR 定位界面、轮询等待状态、读游戏日志确认结果
（`capture_screen` / `ocr` / `probe_log` / `wait_for` / `verify_state`）。

**平台**：只支持 **Windows**。

---

## 第 0 步：确认包根目录的绝对路径

本文件所在目录就是「包根目录」。先拿到它的绝对路径（`pwd` / `Get-Location`，
或你读文件时用的路径）。下面所有 `包根目录` 都指它。
**不要**假设它叫某个固定名字或盘符。

---

## 第 1 步：跑安装脚本

在包根目录下执行：

```powershell
python install.py
```

**为什么用 Python 而不是 .ps1**：Windows 自带的 PowerShell 5.1
（`powershell.exe`）读**无 BOM 的 UTF-8** 中文脚本会按 GBK 解码、直接语法报错；
而 `pwsh`（PowerShell 7）对方不一定装了。对方装了 Python 才能跑 MCP，
所以 **Python 一定在** —— 用它最稳。

脚本会依次做四件事：

1. 检查 Python 版本（需要 3.10+）
2. 装必需依赖 `mcp` + `frida`（**优先用包内 `wheels/required/` 离线装**，不需要外网）
3. 跳过可选弱视觉（除非加 `--vision`）
4. **冒烟测试**：起一次服务器，数注册了多少工具

**装完必须核对这一行**（工具数应 >= 38）：

```
[OK] 服务器正常，注册了 N 个工具
```

### 常见失败与处理

| 现象 | 怎么办 |
|---|---|
| 提示找不到 `python` | 让用户装 Python 3.10+（<https://www.python.org/downloads/>），**务必勾选 "Add python.exe to PATH"**，装完**重开终端**再试 |
| 提示 Python 版本过低 | 同上，需要 3.10 或更高 |
| 依赖安装失败 | 如实报告。可试国内镜像：`python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple mcp frida` |
| 冒烟测试失败 | 把脚本打印的报错**原样**告诉用户；多半是依赖没装全 |
| 想让用户有截图/OCR | 重跑 `python install.py --vision`（装的是 `wheels/optional/` 里的包） |

### 备选：已有 pwsh 7 的用户

```powershell
pwsh -ExecutionPolicy Bypass -File .\\install.ps1
```

> ⚠️ **不要用 `powershell`（5.1）跑这个 .ps1** —— 中文会乱码导致语法错误。
> 只能 `pwsh`。所以默认走 `python install.py`。

---

## 第 2 步：把 MCP 配置写进用户的客户端

配置样例在 `mcp-config/`。**样例里是占位符 `ABSOLUTE_PATH`，必须替换成第 0 步拿到的真实路径。**

| 用户用的客户端 | 该做的 |
|---|---|
| **Claude Code** | 把 `mcp-config/claude-code.mcp.json` 的内容并进用户的 MCP 配置，把 `ABSOLUTE_PATH` 全部替换成包根目录的真实路径 |
| Codex / Cursor / 其它 | 见 `mcp-config/README.md`，格式一样，同样替换路径 |

三个关键字段（**必须是绝对路径**）：

```
命令 : <python 的绝对路径>        ← 第 1 步脚本会打印
参数 : <包根目录>\\src\\gamemod_mcp\\server.py
环境 : PYTHONPATH=<包根目录>\\src
       PYTHONUTF8=1
```

**JSON 里反斜杠要写成 `\\\\`**，或者干脆用正斜杠 `C:/.../src`（Python 也认）。

---

## 第 3 步：验证真的装对了

```powershell
python install.py --check
```

它会只做自检、不装东西，最后打印工具数。输出 `[OK] 服务器可启动，注册了 N 个工具`
就是装对了；报 `ModuleNotFoundError` 或工具数是 0 就回到第 1 步排查。

---

## 第 4 步：告诉用户两件事（不要说漏）

1. **注入游戏进程不需要管理员权限**（实测订正 —— 早先的版本写错了）。
   普通用户权限就能注入普通用户权限的游戏；只要 Agent 与游戏的
   **完整性级别相同**即可。**只有游戏以管理员身份运行时**才需要把 Agent
   也提权。所以默认按普通权限用，**不要一上来就要求用户提权**。
   （真正会失败的是带反作弊 / 反注入的网游，那是保护机制，与权限无关。）
2. **弱视觉（截图 / OCR）是可选的**。不装也能用元数据查询、建索引、Hook、
   读内存等绝大部分功能。

---

## 完成标准（自己核对）

- [ ] `python install.py` 跑完没有报错
- [ ] 工具数 >= 38
- [ ] MCP 配置已写进用户的客户端，且路径是**本机绝对路径**（不是 `ABSOLUTE_PATH`）
- [ ] 已告诉用户「**不需要管理员权限**（除非游戏本身是管理员运行）」和「OCR 是可选的」

**任何一步失败：如实说明失败原因和卡在哪一步，不要跳过、不要假装成功。**

---

## 其它资料

- 完整说明：同目录 `README.md`
- 给 AI 的技能指南：`skills/gamemod-mod/SKILL.md`（装好后也是 MCP 资源 `gamemod://guide`）
- 验收脚本：`devtools/verify_*.py`（可自证「装对了」）

> ⚠️ **合规提醒**：本工具集用于**单机游戏的 Mod 开发与学习研究**。
> 请勿用于在线多人游戏的作弊，也不要用于任何未获授权的目标。
> 按键模拟（`SendInput`）会被带反作弊的网游检测，**只在单机/离线场景使用**。
"""


def write_install_doc(dst: Path, n_tools: int) -> None:
    """写「安装说明.md」（给 AI 读的入口）。

    ⚠️ **不能写死构建机的绝对路径**：对方解压到别处那个路径就不存在了。
    实测踩到过 —— 第一版写死了构建机的路径，发给别人等于给了个不存在的路径。
    所以这里**路径无关**，并要求 AI 第 0 步先自己确认包根目录。

    ⚠️ **不要加「复制这一句话」那一层**：这个目录是**直接交给 Agent** 的，
    Agent 自己会读这份文件。多一层「让人转述给 AI」的说明纯属噪音。
    """
    text = INSTALL_DOC.replace("{tool_hint}", str(n_tools))
    (dst / "安装说明.md").write_text(text, encoding="utf-8", newline="")


# ---------------------------------------------------------------- 离线安装脚本

INSTALL_PS1 = r"""# GameMod MCP 一键安装（Windows）—— 分享包版【备选入口】
#
# ⚠️ **主推 `python install.py`**，本脚本是备选。原因：
#    Windows 自带的 PowerShell 5.1（powershell.exe）读**无 BOM 的 UTF-8**
#    脚本会按 GBK 解码，本文件里的中文会变乱码并**直接语法报错**：
#        The string is missing the terminator: ".
#    所以本脚本**只能用 pwsh（PowerShell 7）跑**，而 pwsh 不一定装了。
#    对方装了 Python 才能跑 MCP，所以 python 一定在 —— 优先用它。
#
# 用法（**必须是 pwsh 7，不能用 powershell 5.1**）：
#     pwsh -ExecutionPolicy Bypass -File .\install.ps1
#     pwsh -ExecutionPolicy Bypass -File .\install.ps1 -WithVision   # 连可选视觉一起装
#
# 与仓库里的 install.ps1 的区别：**优先用本包 wheels/ 里的离线 wheel**，
# 这样对方没有外网也能装上必需依赖。离线装不上再回退到网络。

param([switch]$WithVision)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Src  = Join-Path $Root 'src'
$WheelsReq = Join-Path $Root 'wheels\required'
$WheelsOpt = Join-Path $Root 'wheels\optional'

# 若是 PowerShell 5.1，中文已乱码（语法可能都没过），提前说清
if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Host "错误：本脚本需要 PowerShell 7（pwsh）。" -ForegroundColor Red
    Write-Host "      当前是 PowerShell $($PSVersionTable.PSVersion)（5.1 读中文会乱码）。" -ForegroundColor Red
    Write-Host "      请改用：python install.py" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " GameMod MCP 安装（分享包）" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 中文 Windows 控制台默认 GBK，脚本里打印符号可能崩；统一成 UTF-8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# ---------------------------------------------------------------- 1/5 Python
Write-Host "[1/5] 检查 Python…" -ForegroundColor Yellow

$py = $null
foreach ($cand in @('python', 'python3', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) {
    Write-Host "  x 没找到 Python。请先安装 Python 3.10+ 并勾选 Add to PATH。" -ForegroundColor Red
    Write-Host "    https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
$ver = & $py -c "import sys; print('.'.join(str(x) for x in sys.version_info[:3]))"
$ok  = & $py -c "import sys; print(1 if sys.version_info >= (3,10) else 0)"
if ($ok -ne '1') {
    Write-Host "  x Python 版本过低：$ver（需要 3.10+）" -ForegroundColor Red
    exit 1
}
Write-Host "  v Python $ver" -ForegroundColor Green
Write-Host "    路径: $py"
if ($py -like '*WindowsApps*') {
    Write-Host "  ! 这是 Microsoft Store 的占位程序，可能装不上依赖。" -ForegroundColor Yellow
    Write-Host "    建议装 python.org 的正式版。" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 2/5 必需依赖
Write-Host ""
Write-Host "[2/5] 安装必需依赖（mcp + frida）…" -ForegroundColor Yellow

function Get-ReqSpecs {
    $req = Join-Path $Root 'requirements.txt'
    $specs = @()
    if (Test-Path $req) {
        $extract = @'
import sys
specs = []
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    for line in fh:
        line = line.split("#", 1)[0].strip()
        if line:
            specs.append(line)
sys.stdout.write("\n".join(specs))
'@
        $tmp = Join-Path $env:TEMP ("gm_req_{0}.py" -f (Get-Random))
        Set-Content -Path $tmp -Value $extract -Encoding UTF8
        try { $specs = @(& $py $tmp $req) | Where-Object { $_ -and $_.Trim() } }
        finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }
    if ($specs.Count -eq 0) { $specs = @('mcp>=1.0.0,<2', 'frida>=16.0.0') }
    return $specs
}

$specs = Get-ReqSpecs
$installed = $false

# 先试**离线**（用包内 wheel）
if (Test-Path $WheelsReq) {
    $n = (Get-ChildItem $WheelsReq -Filter *.whl -ErrorAction SilentlyContinue).Count
    if ($n -gt 0) {
        Write-Host "  尝试离线安装（包内 $n 个 wheel，无需外网）…" -ForegroundColor DarkGray
        & $py -m pip install --no-index --find-links $WheelsReq @specs
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  v 离线安装成功" -ForegroundColor Green
            $installed = $true
        } else {
            Write-Host "  离线装不上，回退到网络…" -ForegroundColor Yellow
        }
    }
}
if (-not $installed) {
    Write-Host "  从网络安装: $($specs -join ', ')" -ForegroundColor DarkGray
    & $py -m pip install @specs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  x 依赖安装失败。若在公司网络下，可能需要配置 pip 镜像源。" -ForegroundColor Red
        exit 1
    }
    Write-Host "  v 依赖就绪" -ForegroundColor Green
}

# ---------------------------------------------------------------- 3/5 可选视觉
Write-Host ""
$optSpecs = @('dxcam','mss','rapidocr-onnxruntime','onnxruntime','numpy',
              'opencv-python','pillow','pywin32')
if ($WithVision) {
    Write-Host "[3/5] 安装可选弱视觉（截图 + OCR）…" -ForegroundColor Yellow
    $done = $false
    if (Test-Path $WheelsOpt) {
        $n = (Get-ChildItem $WheelsOpt -Filter *.whl -ErrorAction SilentlyContinue).Count
        if ($n -gt 0) {
            & $py -m pip install --no-index --find-links $WheelsOpt @optSpecs
            if ($LASTEXITCODE -eq 0) { $done = $true; Write-Host "  v 离线安装成功" -ForegroundColor Green }
        }
    }
    if (-not $done) {
        & $py -m pip install @optSpecs
        if ($LASTEXITCODE -eq 0) { $done = $true; Write-Host "  v 安装成功" -ForegroundColor Green }
        else { Write-Host "  ! 弱视觉装不上（不影响其它功能）。可稍后重试。" -ForegroundColor Yellow }
    }
} else {
    Write-Host "[3/5] 跳过可选弱视觉（截图/OCR）。" -ForegroundColor DarkGray
    Write-Host "      要装的话加 -WithVision 再跑一次，或看 安装说明.md 第 1 步。" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- 4/5 冒烟测试
Write-Host ""
Write-Host "[4/5] 冒烟测试：确认工具能注册…" -ForegroundColor Yellow

$smoke = @'
import sys, asyncio
sys.path.insert(0, r"__SRC__")
from gamemod_mcp import server
async def main():
    tools = await server.mcp.list_tools()
    print(f"TOOLCOUNT={len(tools)}")
asyncio.run(main())
'@.Replace('__SRC__', $Src)
$tmp = Join-Path $env:TEMP ("gm_smoke_{0}.py" -f (Get-Random))
Set-Content -Path $tmp -Value $smoke -Encoding UTF8
try {
    $out = & $py $tmp 2>&1
    $line = $out | Where-Object { $_ -match 'TOOLCOUNT=' } | Select-Object -First 1
    if ($line) {
        $ntools = ($line -split '=')[1]
        Write-Host "  v 服务器正常，注册了 $ntools 个工具" -ForegroundColor Green
    } else {
        Write-Host "  x 冒烟测试失败：" -ForegroundColor Red
        $out | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        exit 1
    }
} finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------- 5/5 配置
Write-Host ""
Write-Host "[5/5] 完成。MCP 客户端配置（**路径按你的实际位置**）：" -ForegroundColor Yellow
Write-Host ""
Write-Host "  命令 : $py" -ForegroundColor Gray
Write-Host "  参数 : $Src\gamemod_mcp\server.py" -ForegroundColor Gray
Write-Host "  环境 : PYTHONPATH=$Src" -ForegroundColor Gray
Write-Host "         PYTHONUTF8=1" -ForegroundColor Gray
Write-Host ""
Write-Host "  现成样例见 mcp-config\ 目录（README.md 有各客户端说明）。" -ForegroundColor Gray
Write-Host ""
Write-Host "  提示：注入游戏进程**不需要管理员权限**（实测）。" -ForegroundColor Gray
Write-Host "        只要 Agent 与游戏以相同权限运行即可；" -ForegroundColor Gray
Write-Host "        只有游戏本身以管理员运行时才需要提权。" -ForegroundColor Gray
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " 安装完成" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
"""


def write_install_ps1(dst: Path) -> None:
    (dst / "install.ps1").write_text(INSTALL_PS1, encoding="utf-8", newline="")


# ---------------------------------------------------------------- 自检

def make_zip(pkg: Path) -> Path | None:
    """把分享包压成 zip（放在包目录同级）。失败返回 None。"""
    import zipfile
    out = pkg.parent / (pkg.name + ".zip")
    try:
        if out.exists():
            out.unlink()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as z:
            for p in sorted(pkg.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(pkg.parent))
        return out
    except Exception as exc:                                  # noqa: BLE001
        log(f"  ! 打 zip 失败（不影响目录本身）：{exc}")
        return None


def verify_share(pkg: Path) -> tuple[int, int]:
    """校验分享包完整性。返回 (通过项, 失败项)。"""
    ok = fail = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, fail
        if cond:
            print(f"  [OK]   {label}" + (f" -- {detail}" if detail else ""))
            ok += 1
        else:
            print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))
            fail += 1

    print("=" * 74)
    print("分享包自检")
    print("=" * 74)

    #: 包内**必须**有的文件。注意：
    #:  · 入口是 `安装说明.md`（不是「一句话安装.md」—— 这个目录直接给
    #:    Agent，不需要「让人转述」那一层）；
    #:  · 安装脚本主推 `install.py`；`install.ps1` 是给已有 pwsh 7 的备选；
    #:  · **没有 `install.sh`** —— 本项目只支持 Windows。
    must = ["安装说明.md", "README.md", "install.py", "install.ps1",
            "requirements.txt", "src/gamemod_mcp/server.py",
            "src/gamemod_mcp/runtime/agent.js",
            "skills/gamemod-mod/SKILL.md",
            "mcp-config/README.md", "mcp-config/claude-code.mcp.json"]
    for m in must:
        check(f"存在 {m}", (pkg / m).is_file())

    # 只支持 Windows：不该带 macOS/Linux 的安装脚本
    check("不含 install.sh（本项目只支持 Windows）",
          not (pkg / "install.sh").exists())

    # 安装说明必须能独立指导安装
    one = (pkg / "安装说明.md")
    if one.is_file():
        t = one.read_text(encoding="utf-8")
        check("安装说明.md 给出可执行的安装命令（python install.py）",
              "python install.py" in t)
        check("安装说明.md 解释了「为什么用 Python 而不是 .ps1」",
              "PowerShell 5.1" in t or "pwsh" in t)
        check("安装说明.md 提醒不要用 powershell 5.1 跑 .ps1",
              "不要用" in t and "5.1" in t)
        # ⚠️ **路径无关**是硬要求：写死构建机路径的话，对方解压到别处就失效
        # （第一版就写死了构建机路径，等于给了个不存在的路径）
        check("安装说明.md **不含**构建机的绝对路径（必须路径无关）",
              str(ROOT) not in t and str(pkg) not in t,
              f"不该出现 {ROOT}")
        check("安装说明.md 要求 AI 先确认包根目录",
              "绝对路径" in t and "第 0 步" in t)
        # ⚠️ **不要**出现「复制这一句话给 AI」那种转述层：
        # 这个目录是直接交给 Agent 的，Agent 自己会读。
        check("安装说明.md 没有多余的「转述给 AI」那一层",
              "帮我装好" not in t and "直接复制这一句" not in t)
        # ⚠️ 这条断言**反过来了**：以前它要求说明书里出现「管理员权限」，
        # 就把一个**错误的**说法锁进了产物（实测普通权限即可注入）。
        # 现在要求：必须说明「不需要管理员」，且必须解释唯一的例外
        # （游戏本身以管理员运行时）。这类「把错误结论写成门禁」的坑
        # 比没有门禁更糟 —— 它会阻止别人改对。
        check("安装说明.md 正确说明「不需要管理员权限」",
              "不需要管理员权限" in t or "不需要管理员" in t)
        check("安装说明.md 说明了唯一的提权例外（游戏本身是管理员运行）",
              "完整性级别" in t or "游戏本身" in t or "以管理员身份运行" in t)
        # ⚠️ 不能用 `"需要管理员权限" not in t` 来判「没有遗留错误说法」——
        # 正确表述「**不需要**管理员权限」里**就含**这个子串，会误报
        # （实测踩到过）。要按**行**判断：只揪出「没有否定词」的那种行。
        def _bad_admin_line(line: str) -> bool:
            if "管理员" not in line:
                return False
            # 有这些词 → 是在纠正/限定，不是无条件断言
            for ok in ("不需要", "不用", "不是", "无需", "例外", "权限级",
                       "完整性级别", "只有游戏", "才需要", "not in"):
                if ok in line:
                    return False
            return ("需要管理员" in line or "要管理员" in line
                    or "用管理员身份启动" in line)

        bad_lines = [ln.strip() for ln in t.splitlines()
                     if _bad_admin_line(ln)]
        check("安装说明.md **没有**遗留「必须用管理员」的错误说法",
              not bad_lines,
              f"{bad_lines[:2]}" if bad_lines else "")
        check("安装说明.md 说明了 OCR 可选", "可选" in t)
        check("安装说明.md 含合规提醒（单机/勿用于在线作弊）",
              "反作弊" in t or "合规" in t)

    # install.py 必须能真的跑起来（--check 只自检、不装东西）
    ipy = pkg / "install.py"
    if ipy.is_file():
        r = subprocess.run([sys.executable, str(ipy), "--check"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        check("install.py --check 能跑通（包内路径下）",
              r.returncode == 0 and "个工具" in out,
              out.strip().splitlines()[-1] if out.strip() else
              f"exit={r.returncode}")

    # install.ps1 是备选：它**不能**在 PowerShell 5.1 下跑（中文乱码），
    # 所以包里必须说清这一点，且文件本身要存在（给 pwsh 7 用户）
    ips = pkg / "install.ps1"
    check("install.ps1 存在（给已有 pwsh 7 的用户的备选）", ips.is_file())

    # 离线 wheel
    req = pkg / "wheels" / "required"
    if req.is_dir():
        whls = list(req.glob("*.whl"))
        check("wheels/required 里有 wheel", len(whls) > 0, f"{len(whls)} 个")
        names = " ".join(p.name.lower() for p in whls)
        check("含 frida 的 wheel", "frida" in names)
        check("含 mcp 的 wheel", "mcp" in names)
        total = sum(p.stat().st_size for p in whls) / 1024 / 1024
        check("必需 wheel 体积合理（< 200 MB）", total < 200,
              f"{total:.1f} MB")
    else:
        check("wheels/required 存在（离线安装的前提）", False,
              "没打包 wheel —— 对方需联网")

    opt = pkg / "wheels" / "optional"
    if opt.is_dir():
        n = len(list(opt.glob("*.whl")))
        check("wheels/optional 里有 wheel", n > 0, f"{n} 个")
    else:
        check("wheels/optional 存在", False, "未打包可选依赖")

    # 源码可导入性（用包内 src 真跑一次，这是**最强**的一条）
    src = pkg / "src"
    if src.is_dir():
        n_tools = count_pkg_tools(pkg)
        check("包内源码能被导入并注册工具", n_tools >= 38,
              f"{n_tools} 个工具" if n_tools else "起不来（见上一条命令的输出）")
        # 工具数与包内 README 声明一致（防止拷了半套）
        rm = (pkg / "README.md")
        if rm.is_file() and n_tools:
            rt = rm.read_text(encoding="utf-8")
            import re as _re
            m = _re.search(r"工具一览（(\d+)\s*个）", rt)
            if m:
                check("包内 README 声明数与实际一致",
                      int(m.group(1)) == n_tools,
                      f"README={m.group(1)} 实际={n_tools}")

    # 不该进包的东西
    for bad in ["__pycache__", ".git", ".cache-test", "agent_logs"]:
        hit = any(p for p in pkg.rglob(bad) if p.name == bad)
        check(f"不含 {bad}", not hit)

    # 依赖约束必须挡住 mcp 2.x（打包时实测到的缺陷，加断言防回归）
    reqf = pkg / "requirements.txt"
    if reqf.is_file():
        rt = reqf.read_text(encoding="utf-8")
        # 只看非注释行
        dep_lines = [ln.split("#", 1)[0].strip()
                     for ln in rt.splitlines()]
        mcp_lines = [ln for ln in dep_lines if ln.startswith("mcp")]
        check("requirements.txt 的 mcp 约束带 <2（否则全新环境装到 2.x 会起不来）",
              any("<2" in ln for ln in mcp_lines),
              f"实际：{mcp_lines}")
    ps = pkg / "install.ps1"
    if ps.is_file():
        pt = ps.read_text(encoding="utf-8")
        check("install.ps1 的兜底约束也带 <2",
              "mcp>=1.0.0,<2" in pt, "兜底路径同样会装错版本")

    # 必需 wheel 里不许出现 mcp 2.x（否则离线装照样错）
    if req.is_dir():
        bad_mcp = [p.name for p in req.glob("mcp-2*.whl")]
        check("wheels/required 里没有 mcp 2.x 的 wheel",
              not bad_mcp, f"{bad_mcp}" if bad_mcp else "")

    print("=" * 74)
    print(f"通过 {ok} 项，失败 {fail} 项")
    print("=" * 74)
    return ok, fail


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="打包 GameMod MCP 分享包")
    ap.add_argument("--no-wheels", action="store_true",
                    help="不打依赖 wheel（体积小，但对方要联网）")
    ap.add_argument("--check", action="store_true",
                    help="只校验已有分享包，不重新打包")
    ap.add_argument("--force", action="store_true", help="覆盖已有目录")
    ap.add_argument("--no-zip", action="store_true", help="不打 zip")
    args = ap.parse_args()

    if args.check:
        if not SHARE.is_dir():
            print(f"分享包不存在：{SHARE}")
            return 1
        _, fail = verify_share(SHARE)
        return 1 if fail else 0

    if SHARE.exists():
        if not args.force and any(SHARE.iterdir()):
            print(f"目标已存在且非空：{SHARE}")
            print("加 --force 覆盖，或用 --check 校验它。")
            return 1
        shutil.rmtree(SHARE)
    SHARE.mkdir(parents=True, exist_ok=True)
    log(f"打包到：{SHARE}")

    # 1) 源码与文档
    total = 0
    for d in COPY_DIRS:
        s = ROOT / d
        if not s.is_dir():
            log(f"  ! 跳过不存在的目录 {d}")
            continue
        n = copy_tree(s, SHARE / d)
        total += n
        log(f"  拷贝 {d}/  ({n} 文件)")
    for f in COPY_FILES:
        s = ROOT / f
        if s.is_file():
            shutil.copy2(s, SHARE / f)
            total += 1
    log(f"  共 {total} 个文件")

    # 2) 入口文件
    #    `install.py` 由 COPY_FILES 拷入（主推入口，仓库里的那份就是成品）；
    #    `install.ps1` 覆盖成分享包版（离线优先）；入口说明现写。
    write_install_ps1(SHARE)
    # 工具数从包内源码现算，别写死（写死必然漂）
    n_now = count_pkg_tools(SHARE)
    write_install_doc(SHARE, n_now)
    log(f"  已生成 安装说明.md / install.ps1（离线优先版，工具数 {n_now}）")

    # 2b) 清掉拷进来时可能带上的字节码目录
    pruned = prune_artifacts(SHARE)
    if pruned:
        log(f"  清掉 {len(pruned)} 个字节码产物：{pruned[:3]}")

    # 3) wheel
    if args.no_wheels:
        log("  跳过 wheel（--no-wheels）")
    else:
        log("  下载必需依赖 wheel（mcp + frida）…")
        okr, err = download_wheels(REQUIRED, SHARE / "wheels" / "required")
        if not okr:
            log(f"  x 必需 wheel 下载失败：{err}")
            log("    → 用 --no-wheels 打纯源码包，或检查网络后重试")
            return 1
        log("  下载可选弱视觉 wheel（截图 + OCR）…")
        oko, erro = download_wheels(OPTIONAL, SHARE / "wheels" / "optional")
        if not oko:
            log(f"  ! 可选 wheel 下载失败（不影响必需功能）：{erro}")

    # 4) 自检
    print()
    ok, fail = verify_share(SHARE)
    size = sum(p.stat().st_size for p in SHARE.rglob("*") if p.is_file())
    print(f"\n包体总大小：{size / 1024 / 1024:.1f} MB（{SHARE}）")

    # 5) 打 zip（方便直接发送；也放在仓库同级，不进 git）
    if fail == 0 and not args.no_zip:
        zip_path = make_zip(SHARE)
        if zip_path:
            zs = zip_path.stat().st_size / 1024 / 1024
            print(f"压缩包：{zip_path}（{zs:.1f} MB）")

    print("\n接下来：把目录或压缩包发给对方（**整个目录直接交给他的 AI**），"
          "让 AI 读里面的 安装说明.md 并照做。")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
