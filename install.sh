#!/usr/bin/env bash
# GameMod MCP 一键安装脚本（macOS / Linux）
#
# 用法：
#     bash install.sh
#
# 做三件事：
#     1. 检查 Python 版本
#     2. 装依赖（mcp + frida）
#     3. 冒烟测试：起一次服务器，确认工具能正常注册

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/src"

echo ""
echo "========================================"
echo " GameMod MCP 安装"
echo "========================================"
echo ""

# ---------------------------------------------------------------- 1. Python
echo "[1/4] 检查 Python…"

PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        PY="$cand"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "  ✗ 没找到 Python。请先安装 Python 3.10+。" >&2
    exit 1
fi

VER="$($PY -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    echo "  ✗ Python 版本过低：$VER（需要 3.10+）" >&2
    exit 1
fi
echo "  ✓ Python $VER"
echo "    路径: $(command -v "$PY")"

# ---------------------------------------------------------------- 2. 依赖
echo ""
echo "[2/4] 安装依赖（mcp + frida）…"

# requirements.txt 是 UTF-8（含中文注释）。pip 读 -r 文件时只认 BOM，
# 否则回退到系统区域编码；在中文 Windows 的 GBK 下会 UnicodeDecodeError。
# 这里显式按 UTF-8 解析出包名，再作为参数交给 pip。
REQ="$ROOT/requirements.txt"
SPECS=()
if [ -f "$REQ" ]; then
    while IFS= read -r line; do
        line="${line%%#*}"; line="$(echo "$line" | tr -d '[:space:]')"
        [ -n "$line" ] && SPECS+=("$line")
    done < "$REQ"
fi

if [ ${#SPECS[@]} -gt 0 ]; then
    echo "  待安装: ${SPECS[*]}"
    $PY -m pip install "${SPECS[@]}"
else
    $PY -m pip install 'mcp>=1.0.0' 'frida>=16.0.0'
fi
echo "  ✓ 依赖就绪"

# ---------------------------------------------------------------- 3. 冒烟测试
echo ""
echo "[3/4] 冒烟测试：确认工具能正常注册…"

TMP="$(mktemp -t gamemod_smoke_XXXXXX.py)"
cat > "$TMP" <<PYEOF
import sys, asyncio
sys.path.insert(0, r"$SRC")
from gamemod_mcp import server
async def main():
    tools = await server.mcp.list_tools()
    print(f"TOOLCOUNT={len(tools)}")
asyncio.run(main())
PYEOF

OUT="$(PYTHONPATH="$SRC" $PY "$TMP" 2>&1 || true)"
rm -f "$TMP"

LINE="$(printf '%s\n' "$OUT" | grep -m1 'TOOLCOUNT=' || true)"
if [ -n "$LINE" ]; then
    echo "  ✓ 服务器正常，注册了 ${LINE#TOOLCOUNT=} 个工具"
else
    echo "  ✗ 冒烟测试失败，输出如下：" >&2
    printf '%s\n' "$OUT" | sed 's/^/    /' >&2
    exit 1
fi

# ---------------------------------------------------------------- 4. 配置提示
echo ""
echo "[4/4] 完成。接下来配置你的 Agent："
echo ""
echo "  在 MCP 客户端配置里填："
echo "    命令 : $PY"
echo "    参数 : $SRC/gamemod_mcp/server.py"
echo "    环境 : PYTHONPATH=$SRC"
echo ""
echo "  各客户端的现成配置在 mcp-config/ 目录，详见 mcp-config/README.md"
echo ""

case "$(uname -s)" in
    Darwin)
        echo "  ⚠ macOS 上注入进程需要关闭 SIP 或给终端授予调试权限；"
        echo "    系统完整性保护会阻止注入其它进程。"
        ;;
    Linux)
        echo "  ⚠ Linux 上注入需要 ptrace 权限："
        echo "    sudo sysctl -w kernel.yama.ptrace_scope=0"
        ;;
esac
echo ""
echo "========================================"
echo " 安装完成"
echo "========================================"
