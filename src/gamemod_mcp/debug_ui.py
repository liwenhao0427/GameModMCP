"""人工调试前端：在浏览器里查看并调用**全部** MCP 工具。

定位（与业务功能的关系）
============================================================
这是**旁路调试设施**，不是业务能力：

· 它**不重新实现**任何工具 —— 工具清单直接问 `FastMCP.list_tools()`，
  调用直接走 `FastMCP.call_tool()`。所以前端里看到的参数表、返回体
  与 AI 客户端拿到的是**同一份**，不存在「前端能调通、AI 调不通」的漂移。
· 它**不改业务逻辑**：对 `server.py` 的唯一改动是分发层一个可关闭的
  流水钩子（见 `debuglog` 与 `_install_call_logger`）。
· 项目**运行期依赖不变**：只用标准库 `http.server`，不引入 Flask/FastAPI/Node。

为什么用标准库而不是现成的 Web 框架
------------------------------------------------------------
`AGENTS.md` §2.4 规定新增第三方依赖要先说明理由。这里没有理由：
需求只是「静态页 + 十来个 JSON 接口」，`ThreadingHTTPServer` 足够。
引入框架的代价（依赖、版本、打包）远大于收益。

两个必须做对的地方
------------------------------------------------------------
1. **单事件循环**。MCP 服务器本来是「一个长期存活的循环」。如果在每个
   HTTP 请求里 `asyncio.run(...)`，就会为同一个运行时会话不断创建新循环 ——
   而 Frida 会话里可能有绑定到原循环的东西。所以这里起**一个**后台线程
   跑**一个**循环，所有调用都 `run_coroutine_threadsafe` 提交给它，
   与服务器的真实形态一致。
2. **调用必须可回溯**。前端发出的核对流水里标 `source="ui"`，
   AI 客户端发的标 `source="mcp"`（见 `debuglog.set_source`）。

启动
------------------------------------------------------------
    python devtools\\debug_ui.py            # 等价于 dbg_ui 入口
    gamemod-mcp-dbg-ui                     # 装过包之后

默认只监听 `127.0.0.1`，且**不是** MCP 协议端点：它是给人看的页面。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# 允许 `python src/gamemod_mcp/debug_ui.py` 直接跑
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from gamemod_mcp import debuglog as dlog
    from gamemod_mcp import server as S
    from gamemod_mcp.logutil import warn
else:
    from . import debuglog as dlog
    from . import server as S
    from .logutil import warn

#: 单次调用等待上限（秒）。`launch_game` 这类要起进程的操作可能很久，
#: 所以给得比较宽；真超时了如实报错，不静默返回半截结果。
DEFAULT_TIMEOUT = 300.0

_HTML_FILE = Path(__file__).with_name("debug_ui.html")


# ================================================================
# 单事件循环执行器
# ================================================================


class LoopRunner:
    """在一个后台线程里跑**一个**长期事件循环，供所有调用复用。

    为什么不用 `asyncio.run()` 每次新建循环：见模块 docstring 第 1 条。
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve, name="gamemod-debug-loop", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """把协程提交给那个循环执行并等结果（可超时）。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)


_runner: LoopRunner | None = None
_runner_lock = threading.Lock()


def runner() -> LoopRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = LoopRunner()
        return _runner


# ================================================================
# 工具清单
# ================================================================


def tool_groups() -> dict[str, str]:
    """从 `server.py` 的模块 docstring 里解析「工具分层」。

    为什么不自己另写一份分组表：那份清单**已经**被
    `devtools/verify_tool_inventory.py` 双向卡住（与代码里的工具集合完全一致），
    再抄一份就是第二个真相来源，必然漂移。解析不出来就整体归入「未分组」，
    由前端如实显示 —— 不隐藏工具。
    """
    text = S.__doc__ or ""
    section = ""
    m = re.search(r"工具分层\s*\n-+\s*\n(.*?)(?:\n\S)", text, re.S)
    if m:
        section = m.group(1)

    groups: dict[str, str] = {}
    current = ""
    for raw in section.splitlines():
        line = raw.strip()
        if not line:
            continue
        head = re.match(r"^\d+\.\s*([^:]+?)\s*:\s*(.*)$", line)
        if head:
            current = head.group(1).strip()
            rest = head.group(2)
        else:
            rest = line           # 续行：同一层的后续工具
        if not current:
            continue
        for name in re.split(r"[/、,]", rest):
            name = name.strip()
            if name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                groups[name] = current
    return groups


def tools_snapshot() -> dict:
    """全部工具的目录：名称、说明、语义标注、入参 schema、所属层。"""
    try:
        tools = runner().run(S.mcp.list_tools(), timeout=30)
    except Exception as exc:      # noqa: BLE001
        return {"ok": False, "error": f"取工具清单失败：{exc}"}

    groups = tool_groups()
    items: list[dict] = []
    for t in tools:
        ann = getattr(t, "annotations", None)
        schema = getattr(t, "inputSchema", None) or {}
        items.append({
            "name": t.name,
            "title": getattr(ann, "title", "") or "",
            "description": (t.description or "").strip(),
            "group": groups.get(t.name, "未分组"),
            "read_only": bool(getattr(ann, "readOnlyHint", False)),
            "destructive": bool(getattr(ann, "destructiveHint", True)),
            "idempotent": bool(getattr(ann, "idempotentHint", False)),
            "open_world": bool(getattr(ann, "openWorldHint", True)),
            "schema": schema,
            "params": _params_of(schema),
        })
    items.sort(key=lambda x: (x["group"], x["name"]))

    ungrouped = [i["name"] for i in items if i["group"] == "未分组"]
    return {
        "ok": True,
        "count": len(items),
        "groups": sorted({i["group"] for i in items}),
        "ungrouped": ungrouped,
        "note": ("有工具没在 server.py 的「工具分层」里列出，已归入「未分组」"
                 "（不隐藏）" if ungrouped else ""),
        "tools": items,
    }


def _params_of(schema: dict) -> list[dict]:
    """把 JSON Schema 摊平成前端好渲染的一行行参数。"""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out: list[dict] = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        typ = spec.get("type", "string")
        enum = spec.get("enum")
        out.append({
            "name": name,
            "type": typ,
            "required": name in required,
            "default": spec.get("default", None),
            "has_default": "default" in spec,
            "description": (spec.get("description") or "").strip(),
            "enum": enum if isinstance(enum, list) else None,
        })
    return out


# ================================================================
# 调用工具
# ================================================================


def _coerce(spec: dict, value: Any) -> tuple[Any, str]:
    """按 schema 把前端传来的值转成正确类型。

    前端输入框给的是字符串，而工具签名要真 int / bool / float。
    转不动就**如实报错**，不把 `"abc"` 硬塞成 0（那会变成假成功）。
    """
    typ = spec.get("type", "string")
    if isinstance(value, str):
        text = value
        if typ == "integer":
            if text.strip() == "":
                return None, "需要整数"
            try:
                return int(text.strip()), ""
            except ValueError:
                return None, f"不是整数：{text!r}"
        if typ == "number":
            if text.strip() == "":
                return None, "需要数字"
            try:
                return float(text.strip()), ""
            except ValueError:
                return None, f"不是数字：{text!r}"
        if typ == "boolean":
            low = text.strip().lower()
            if low in ("true", "1", "yes", "y", "on"):
                return True, ""
            if low in ("false", "0", "no", "n", "off"):
                return False, ""
            return None, f"不是布尔：{text!r}"
        if typ in ("object", "array"):
            if text.strip() == "":
                return ({} if typ == "object" else []), ""
            try:
                return json.loads(text), ""
            except (ValueError, TypeError):
                return None, f"不是合法 JSON：{text[:60]!r}"
        return value, ""
    # 已经是 JSON 原生类型
    return value, ""


def invoke(name: str, raw_args: dict) -> dict:
    """像客户端一样调用一个工具，并把真实返回交给前端。

    走的是 `FastMCP.call_tool` —— 与 AI 客户端**同一条路**，
    因此参数校验、`_cap_response` 的体积上限、事件循环 offload
    都真实经过（前端不会看到比客户端更好的结果）。
    """
    snap = {t["name"]: t for t in (tools_snapshot().get("tools") or [])}
    tool = snap.get(name)
    if tool is None:
        return {"ok": False, "error": f"没有这个工具：{name}"}

    specs = {p["name"]: p for p in tool["params"]}
    args: dict[str, Any] = {}
    problems: list[str] = []
    for key, value in (raw_args or {}).items():
        spec = specs.get(key)
        if spec is None:
            problems.append(f"未知参数：{key}")
            continue
        if isinstance(value, str) and value.strip() == "" and spec["has_default"]:
            continue                      # 空着 = 用默认值，最省事的正确行为
        coerced, why = _coerce(spec, value)
        if why:
            problems.append(f"{key}：{why}")
            continue
        args[key] = coerced

    missing = [p["name"] for p in tool["params"]
               if p["required"] and p["name"] not in args]
    if missing:
        problems.append("缺少必填参数：" + "、".join(missing))
    if problems:
        return {"ok": False, "error": "；".join(problems), "args": args}

    token = dlog.set_source("ui")         # 流水里标成「人手工调的」
    started = time.time()
    try:
        result = runner().run(
            S.mcp.call_tool(name, args), timeout=DEFAULT_TIMEOUT)
        payload = S._tool_payload(result)
    except Exception as exc:              # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "args": args,
            "elapsed_ms": round((time.time() - started) * 1000, 1),
            "hint": ("超时" if isinstance(exc, TimeoutError) else ""),
        }
    finally:
        dlog.reset_source(token)

    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return {
        "ok": True,
        "tool": name,
        "args": args,
        "elapsed_ms": round((time.time() - started) * 1000, 1),
        "json": body,
        "result_ok": payload.get("ok") if isinstance(payload, dict) else None,
        "error": (payload.get("error", "") if isinstance(payload, dict) else ""),
        "bytes": len(body.encode("utf-8", "replace")),
        "capped": bool(payload.get("capped")) if isinstance(payload, dict)
        else False,
        "result": payload,
    }


# ================================================================
# HTTP 层
# ================================================================


def _read_html() -> bytes:
    try:
        return _HTML_FILE.read_bytes()
    except OSError as exc:
        return (f"<h1>找不到界面文件</h1><p>{_HTML_FILE}</p><p>{exc}</p>"
                ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "GameModDebugUI"
    protocol_version = "HTTP/1.1"

    # ---- 工具方法

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass          # 前端刷新导致的中断，不是故障

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _body(self) -> tuple[dict, str]:
        """读请求体。返回 `(解析后的 dict, 错误说明)`。

        坏 JSON 要**如实报错**，不能悄悄当成空 body —— 否则前端传错格式时
        只会收到「没有这个工具：」（工具名也是空的），看起来像工具不存在，
        与真实原因（请求体坏了）完全不同，排查会被带偏。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}, ""
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}, ""
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, TypeError) as exc:
            return {}, f"请求体不是合法 JSON：{exc}"
        if not isinstance(parsed, dict):
            return {}, f"请求体必须是 JSON 对象，收到 {type(parsed).__name__}"
        return parsed, ""

    # ---- 路由

    def do_GET(self) -> None:            # noqa: N802 - BaseHTTPRequestHandler 约定
        url = urlparse(self.path)
        path = url.path
        query = parse_qs(url.query)

        if path in ("/", "/index.html"):
            self._send(200, _read_html(), "text/html; charset=utf-8")
            return
        if path == "/api/tools":
            self._json(tools_snapshot())
            return
        if path == "/api/session":
            self._json({"ok": True, "session": S.get_session()})
            return
        if path == "/api/log/stats":
            self._json(dlog.stats())
            return
        if path == "/api/log/summary":
            self._json(dlog.summary())
            return
        if path == "/api/log/records":
            def num(key: str, default: int) -> int:
                try:
                    return int(query.get(key, [default])[0])
                except (TypeError, ValueError):
                    return default
            self._json(dlog.read_records(
                limit=num("limit", 100),
                offset=num("offset", 0),
                tool=query.get("tool", [""])[0],
                status=query.get("status", [""])[0],
                q=query.get("q", [""])[0],
            ))
            return
        self._json({"ok": False, "error": f"没有这个路径：{path}"}, 404)

    def do_POST(self) -> None:           # noqa: N802
        path = urlparse(self.path).path
        body, body_err = self._body()
        if body_err:
            self._json({"ok": False, "error": body_err}, 400)
            return

        if path == "/api/call":
            name = str(body.get("tool") or "")
            args = body.get("args") if isinstance(body.get("args"), dict) else {}
            self._json(invoke(name, args))
            return
        if path == "/api/log/enabled":
            value = body.get("enabled")
            if not isinstance(value, bool):
                self._json({"ok": False,
                            "error": "enabled 必须是 true/false"}, 400)
                return
            self._json({"ok": True, "enabled": dlog.set_enabled(value)})
            return
        if path == "/api/log/clear":
            self._json(dlog.clear())
            return
        self._json({"ok": False, "error": f"没有这个路径：{path}"}, 404)

    def log_message(self, fmt: str, *args: Any) -> None:
        """访问日志走 stderr —— 绝不能进 stdout（那边可能是 JSON-RPC）。"""
        if self.server.verbose:          # type: ignore[attr-defined]
            warn(f"[debug-ui] {self.address_string()} {fmt % args}")


def make_server(host: str, port: int, verbose: bool = False
                ) -> ThreadingHTTPServer:
    """建好 HTTP 服务器（未启动），便于验收脚本用 0 端口起临时实例。"""
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.verbose = verbose              # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gamemod-mcp-dbg-ui",
        description="GameMod MCP 的人工调试前端（旁路设施，不影响 MCP 业务）")
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址（默认 127.0.0.1，仅本机）")
    p.add_argument("--port", type=int, default=8770, help="监听端口（默认 8770）")
    p.add_argument("--verbose", action="store_true", help="打印 HTTP 访问日志")
    p.add_argument("--log-on", action="store_true",
                   help="本次启动即打开调用流水（等价 GAMEMOD_DEBUG_LOG=1）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.log_on:
        dlog.set_enabled(True)

    httpd = make_server(args.host, args.port, verbose=args.verbose)
    url = f"http://{args.host}:{httpd.server_port}/"
    # 给人看的启动信息走 stderr（stdout 保持干净，与 MCP 一侧的约定一致）
    warn(f"GameMod MCP 调试前端已启动：{url}")
    warn(f"  工具数：{(tools_snapshot().get('count'))}")
    warn(f"  调用流水：{'开' if dlog.is_enabled() else '关'}"
         f"（{dlog.log_path()}）")
    warn("  停止：Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        warn("收到 Ctrl+C，正在停止…")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
