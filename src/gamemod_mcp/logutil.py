"""日志工具：统一走 stderr。

MCP 协议规定 stdout 只能用于 JSON-RPC 帧，任何日志写入 stdout 都会破坏协议。
因此这里所有输出一律走 stderr。
"""

from __future__ import annotations

import logging
import os
import sys

_LOGGER_NAME = "gamemod"

# 级别由环境变量控制，默认 INFO
_LEVEL = os.environ.get("GAMEMOD_LOG_LEVEL", "INFO").upper()


def _build_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:  # 避免重复添加 handler
        return logger

    logger.setLevel(getattr(logging, _LEVEL, logging.INFO))
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="[gamemod] %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log = _build_logger()


def progress(message: str) -> None:
    """输出一条进度信息（走 stderr，供 Agent 观察长任务进展）。"""
    log.info(message)


def warn(message: str) -> None:
    """输出一条警告。"""
    log.warning(message)


def error(message: str) -> None:
    """输出一条错误。"""
    log.error(message)
