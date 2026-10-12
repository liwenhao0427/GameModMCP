"""MCP 工具调用的**调试流水**：可开关、可查看、可清理。

为什么需要它
============================================================
人工调试前端（`debug_ui.py`）要能回答一个回溯性问题：
**「AI 到底调了什么、参数是什么、返回了什么？」**

这件事有个容易做错的地方：如果日志记在**前端里**，那就只能看见
「经我这个前端发出的调用」—— 而 AI 客户端（DSH / Claude Code 等）
自己启动的那个 MCP 服务器进程，前端根本看不见。
所以日志点必须落在 **服务器分发层**（`server.py` 的 `_install_call_logger`），
前端只负责查看与清理。这样人工手调的与 AI 调的会进**同一份**流水，
靠 `source` / `pid` 区分。

三条硬约束（都来自本项目已有的教训）
============================================================
1. **绝不影响业务**。记录失败只能被吞掉并降级为「不记」，
   不许让一个本来正常的工具失败 —— 与 `cap.py` 里「附加保护出问题
   不许把功能弄坏」同一条原则。所以对外 API 全部不抛异常。
2. **默认关闭**。这是调试设施，不是业务能力：默认开着会让每个用户的
   磁盘被慢慢写满。要看 AI 的流水，在那个进程上设 `GAMEMOD_DEBUG_LOG=1`。
3. **不许无界增长**。单文件超过上限就轮转，只保留最近 N 份；
   单条记录里的参数/返回超过上限就**截断并如实标注** `truncated`，
   而不是把一份 200 KB 的 `search_classes` 原样塞进日志。

开关与路径
============================================================
| 环境变量 | 作用 | 默认 |
|---|---|---|
| `GAMEMOD_DEBUG_LOG` | `1/on/true` 开，`0/off/false` 关 | 关 |
| `GAMEMOD_DEBUG_LOG_PATH` | 流水文件路径 | `~/.gamemod/debug/calls.jsonl` |
| `GAMEMOD_DEBUG_LOG_MAX_BYTES` | 单文件上限（超过则轮转） | 8 MB |
| `GAMEMOD_DEBUG_LOG_MAX_FILES` | 保留份数（含当前） | 4 |

运行期还可以用 `set_enabled()` 临时开关（前端里那个开关走的就是它）。
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

#: 单条记录里参数 / 返回各自的字符上限。超过就截断并标 `truncated`。
#: 为什么按字符而不是字节：这里只需要「够看清调了什么」，精确体积
#: 另有 `args_bytes` / `result_bytes` 如实记录。
_MAX_FIELD_CHARS = 8000

#: 读取尾部时的默认窗口（用于「查看最近 N 条」）。日志本身有轮转上限，
#: 所以这里不需要一次读完整份。
_DEFAULT_TAIL_BYTES = 4 * 1024 * 1024

_lock = threading.Lock()

#: 调用来源。默认 `mcp`（经 MCP 协议来的调用）。
#: 调试前端在调用前把它设成 `ui`，于是流水里能区分
#: 「人手工点的」与「AI 调的」—— 这正是回溯时最想要的那一列。
_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gamemod_debug_source", default="mcp")

#: 运行期开关覆盖。None = 跟随环境变量。
_enabled_override: bool | None = None


# ---------------------------------------------------------------- 配置


def _env_flag(name: str, default: bool = False) -> bool:
    """读一个布尔环境变量；认不出来就用默认值（不猜）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "on", "true", "yes", "y")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def log_path() -> Path:
    """流水文件路径（可用 `GAMEMOD_DEBUG_LOG_PATH` 重定向）。"""
    raw = os.environ.get("GAMEMOD_DEBUG_LOG_PATH", "").strip()
    if raw:
        return Path(raw)
    base = Path(os.environ.get("GAMEMOD_HOME", "")) if \
        os.environ.get("GAMEMOD_HOME") else Path.home() / ".gamemod"
    return base / "debug" / "calls.jsonl"


def max_bytes() -> int:
    """单文件上限（字节）。"""
    return _env_int("GAMEMOD_DEBUG_LOG_MAX_BYTES", 8 * 1024 * 1024)


def max_files() -> int:
    """保留的份数（含当前份）。"""
    return _env_int("GAMEMOD_DEBUG_LOG_MAX_FILES", 4)


def is_enabled() -> bool:
    """当前是否在记录。运行期覆盖优先于环境变量。"""
    if _enabled_override is not None:
        return _enabled_override
    return _env_flag("GAMEMOD_DEBUG_LOG", default=False)


def set_enabled(value: bool) -> bool:
    """运行期开关（前端那个开关走这里）。返回设置后的状态。"""
    global _enabled_override
    with _lock:
        _enabled_override = bool(value)
    return is_enabled()


def set_source(source: str) -> contextvars.Token:
    """标记「接下来的调用是谁发起的」。返回 token，可用 `reset_source` 还原。"""
    return _source.set(source or "mcp")


def reset_source(token: contextvars.Token) -> None:
    try:
        _source.reset(token)
    except (ValueError, LookupError):
        pass


def current_source() -> str:
    try:
        return _source.get()
    except LookupError:      # pragma: no cover - 极端情况下不抛
        return "mcp"


# ---------------------------------------------------------------- 写入


def _truncate_text(value: Any, limit: int = _MAX_FIELD_CHARS) -> tuple[Any, bool]:
    """把一段可能很大的值压到上限内，并如实报告是否截断过。

    非字符串（dict/list）先转成 JSON 文本；转不动就原样返回并标未截断
    （`json.dumps` 由调用方再兜一层 `default=str`）。
    """
    if value is None:
        return None, False
    if isinstance(value, str):
        text = value
        if len(text) <= limit:
            return text, False
        return text[:limit] + f"…[截断，原文 {len(text)} 字符]", True
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)[:limit], True
    if len(text) <= limit:
        # 没超上限就保留结构，前端好展开看
        return value, False
    return text[:limit] + f"…[截断，原文 {len(text)} 字符]", True


def _json_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str)
                   .encode("utf-8", "replace"))
    except (TypeError, ValueError):
        return -1


def _rotate(path: Path) -> None:
    """单文件超限：`calls.jsonl` → `calls.jsonl.1` → `.2` …，丢弃最旧的。"""
    keep = max_files()
    try:
        if keep <= 1:
            path.unlink(missing_ok=True)
            return
        oldest = path.with_name(path.name + f".{keep - 1}")
        oldest.unlink(missing_ok=True)
        for idx in range(keep - 2, 0, -1):
            src = path.with_name(path.name + f".{idx}")
            if src.exists():
                src.replace(path.with_name(path.name + f".{idx + 1}"))
        path.replace(path.with_name(path.name + ".1"))
    except OSError:
        # 轮转失败不是业务问题：最差就是这一份继续变大，不抛。
        pass


def record(tool: str, args: Any, result: Any = None, *, ok: Any = None,
           error: str = "", elapsed_ms: float | None = None,
           result_bytes: int | None = None, note: str = "") -> None:
    """记一条调用流水。

    **本函数永不抛异常**：记不上就算了（磁盘满 / 权限不足等），
    绝不能让一个本来正常的工具因为「日志写不进去」而失败。
    """
    try:
        if not is_enabled():
            return
        path = log_path()
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "ts_ms": int(time.time() * 1000),
            "pid": os.getpid(),
            "source": current_source(),
            "tool": tool,
            "ok": ok,
            "error": error or "",
            "elapsed_ms": round(elapsed_ms, 1) if elapsed_ms is not None else None,
        }

        stored_args, args_trunc = _truncate_text(args)
        entry["args"] = stored_args
        entry["args_bytes"] = _json_bytes(args)

        # 返回值：先抽 ok / error 便于筛选，再存（可能被截断的）正文
        stored_result, res_trunc = _truncate_text(result)
        entry["result"] = stored_result
        entry["result_bytes"] = (_json_bytes(result) if result_bytes is None
                                else result_bytes)
        if args_trunc or res_trunc:
            entry["truncated"] = True
        if note:
            entry["note"] = note

        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        payload = line.encode("utf-8", "replace")

        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.exists() and path.stat().st_size >= max_bytes():
                    _rotate(path)
            except OSError:
                pass
            # 单次 O_APPEND 写整行：多进程并发时不容易写串行
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
    except BaseException:        # noqa: BLE001 - 见 docstring：绝不外抛
        return


# ---------------------------------------------------------------- 读取


def _rotated_files(path: Path) -> list[Path]:
    """当前份 + 各轮转份，从**新到旧**排序（便于「最近 N 条」优先）。"""
    out = [path]
    for idx in range(1, max_files()):
        cand = path.with_name(path.name + f".{idx}")
        if cand.exists():
            out.append(cand)
    return out


def tail_bytes() -> int:
    return _env_int("GAMEMOD_DEBUG_LOG_TAIL_BYTES", _DEFAULT_TAIL_BYTES)


def read_records(limit: int = 200, offset: int = 0, tool: str = "",
                 status: str = "", q: str = "") -> dict:
    """读最近若干条流水（**最新在前**），可按工具 / 状态 / 子串过滤。

    Args:
        limit:  本次最多返回多少条。
        offset: 先跳过多少条（已经过滤后的序号），用于翻页。
        tool:   工具名子串过滤（大小写不敏感）。
        status: `ok` / `fail` / 空。
        q:      在工具名 / error / 参数 / 返回文本里找子串。

    Returns:
        {ok, path, exists, returned, total_matched, has_more, files,
         scanned_bytes, truncated_head, records: [...]}
        `exists=False` 表示还没有任何流水（不是故障）。
    """
    path = log_path()
    files = _rotated_files(path)
    existing = [f for f in files if f.is_file()]
    if not existing:
        return {"ok": True, "path": str(path), "exists": False,
                "returned": 0, "total_matched": 0, "has_more": False,
                "files": [], "scanned_bytes": 0,
                "truncated_head": False, "records": []}

    # 逐份从新到旧读，够 limit+offset 就停 —— 不为了翻一页读完整份历史
    want = max(1, limit) + max(0, offset)
    matched: list[dict] = []
    scanned = 0
    truncated_head = False
    needle = q.strip().lower()
    tool_needle = tool.strip().lower()
    status_needle = status.strip().lower()

    for file in existing:
        if len(matched) >= want:
            break
        try:
            size = file.stat().st_size
            with open(file, "rb") as fh:
                if size > tail_bytes():
                    fh.seek(size - tail_bytes())
                    truncated_head = True
                blob = fh.read()
            scanned += len(blob)
        except OSError:
            continue
        lines = blob.decode("utf-8", "replace").splitlines()
        if truncated_head and lines:
            lines = lines[1:]        # 首行可能被截断，丢掉
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue          # 坏行（例如多进程写串了）跳过，不报错
            if not isinstance(rec, dict):
                continue
            if tool_needle and tool_needle not in str(rec.get("tool", "")).lower():
                continue
            if status_needle in ("ok", "true") and rec.get("ok") is not True:
                continue
            if status_needle in ("fail", "failed", "false") \
                    and rec.get("ok") is not False:
                continue
            if needle:
                hay = " ".join([
                    str(rec.get("tool", "")), str(rec.get("error", "")),
                    json.dumps(rec.get("args"), ensure_ascii=False, default=str),
                    json.dumps(rec.get("result"), ensure_ascii=False, default=str),
                ]).lower()
                if needle not in hay:
                    continue
            matched.append(rec)
            if len(matched) >= want:
                break

    page = matched[offset:offset + max(1, limit)]
    return {
        "ok": True,
        "path": str(path),
        "exists": True,
        "returned": len(page),
        "total_matched": len(matched),
        "has_more": len(matched) >= want,
        "files": [str(f) for f in existing],
        "scanned_bytes": scanned,
        "truncated_head": truncated_head,
        "records": page,
    }


def summary() -> dict:
    """汇总：总条数、失败数、按工具计数、按来源计数。

    会**扫完整份当前文件**（轮转上限内），所以同时如实给出
    `scanned_bytes` / `scanned_lines` —— 让调用方知道这个结论
    是在多大范围上得出的，而不是一个来路不明的数字。
    """
    path = log_path()
    files = [f for f in _rotated_files(path) if f.is_file()]
    by_tool: dict[str, dict[str, int]] = {}
    by_source: dict[str, int] = {}
    total = 0
    failed = 0
    scanned_bytes = 0
    first_ts = last_ts = ""

    for file in files:
        try:
            with open(file, "rb") as fh:
                blob = fh.read()
        except OSError:
            continue
        scanned_bytes += len(blob)
        for line in blob.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            total += 1
            name = str(rec.get("tool", "?"))
            slot = by_tool.setdefault(name, {"calls": 0, "failed": 0})
            slot["calls"] += 1
            if rec.get("ok") is False:
                failed += 1
                slot["failed"] += 1
            src = str(rec.get("source", "mcp"))
            by_source[src] = by_source.get(src, 0) + 1
            ts = str(rec.get("ts", ""))
            if ts:
                if not first_ts:
                    first_ts = ts
                last_ts = ts

    return {
        "ok": True,
        "enabled": is_enabled(),
        "path": str(path),
        "exists": bool(files),
        "total": total,
        "failed": failed,
        "by_tool": dict(sorted(by_tool.items(),
                               key=lambda kv: -kv[1]["calls"])),
        "by_source": by_source,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "scanned_bytes": scanned_bytes,
        "files": [str(f) for f in files],
        "max_bytes": max_bytes(),
        "max_files": max_files(),
    }


def stats() -> dict:
    """轻量状态（不扫全文）：开关、路径、体积、份数。"""
    path = log_path()
    files = [f for f in _rotated_files(path) if f.is_file()]
    total = 0
    current = 0
    for idx, f in enumerate(files):
        try:
            size = f.stat().st_size
        except OSError:
            continue
        total += size
        if f == path:
            current = size
    return {
        "ok": True,
        "enabled": is_enabled(),
        "path": str(path),
        "exists": path.is_file(),
        "current_bytes": current,
        "total_bytes": total,
        "files": [str(f) for f in files],
        "max_bytes": max_bytes(),
        "max_files": max_files(),
        "source": current_source(),
    }


def clear() -> dict:
    """清空流水：删掉当前份与全部轮转份。

    Returns:
        {ok, removed: [...], failed: [...]}
        **删不掉的文件如实列在 `failed` 里**，不把「没删掉」报成成功。
    """
    path = log_path()
    removed: list[str] = []
    failed: list[str] = []
    for f in _rotated_files(path):
        if not f.exists():
            continue
        try:
            f.unlink()
            removed.append(str(f))
        except OSError as exc:
            failed.append(f"{f}（{exc}）")
    return {"ok": not failed, "removed": removed, "failed": failed,
            "path": str(path)}
