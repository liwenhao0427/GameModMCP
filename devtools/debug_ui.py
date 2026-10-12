#!/usr/bin/env python3
"""启动 GameMod MCP 的人工调试前端。

用法：
    python devtools\\debug_ui.py                 # http://127.0.0.1:8770/
    python devtools\\debug_ui.py --port 9000
    python devtools\\debug_ui.py --log-on        # 本次启动就打开调用流水

它做的事只有一件：把仓库的 `src` 加进 `sys.path`（路径由本文件位置推导，
不硬编码 —— 见 `samples.py` 的同一条规矩），然后把入口交给
`gamemod_mcp.debug_ui.main`。

与 MCP 业务的关系：**旁路**。它不起 stdio、不占 MCP 端口、
不改任何工具实现；关掉它，MCP 一侧完全不受影响。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001 - 老环境没有 reconfigure
    pass

from gamemod_mcp.debug_ui import main     # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
