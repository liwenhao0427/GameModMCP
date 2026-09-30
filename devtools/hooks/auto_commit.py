#!/usr/bin/env python3
"""DSH Stop hook：回合结束时自动把当前项目提交进 git。

为什么需要这个文件
============================================================
「每回合结束都提交」这件事，**不能靠 Agent 自觉**——它会被忘记，
或者在长任务里被推迟到「全部做完再提交」，于是中间状态全部丢失。

DSH 内置了 Claude Code hooks 桥接（`@deepseek-ai/dsh-hooks-claude-code`），
其中 `Stop` 事件在「运行即将停止时」触发，正好等于「回合结束」。
把本脚本挂到 Stop 上，提交就由**外部机制**保证，与 Agent 记性无关。

设计要点
============================================================
1. **只作用于 opt-in 的项目**
   hook 是全局的（整个 DSH 进程一份配置），会在**任何**项目里触发。
   如果无条件 `git add -A`，就会把别的仓库一起提交——那是事故。
   因此本脚本只对「项目根存在哨兵文件 `.dsh-autocommit.json`」的项目生效。

2. **永不阻断回合**
   一律 exit 0。Stop 的阻塞语义是「强制再执行一步」，
   若因为提交失败而阻塞，会造成回合反复重试甚至死循环。

3. **并发安全**
   同一仓库可能同时有多个会话，用 `.git` 下的排他锁串行化。
   抢不到锁就跳过（下一回合还会提交），不等待。

4. **提交信息可指定**
   若项目根有 `.dsh-autocommit-msg`，取其内容当提交信息并删除该文件。
   否则按变更文件自动生成一条能看懂的摘要。
   这样 Agent 可以给出有意义的中文提交信息，而不是清一色的「自动提交」。

5. **stdout 保持干净**
   只写 stderr。hook 的 stdout 若被当作决策载荷解析，输出杂质会引发意外。

用法（通常不需要手动跑）
============================================================
    echo '{}' | python devtools/hooks/auto_commit.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ---- 强制 stderr 用 UTF-8 ----
# Windows 上 Python 默认按 ANSI 代码页（本机实测 cp936/gbk）编码 stderr，
# 而 DSH 桥接按 UTF-8 读取 hook 的 stderr，中文日志会变成乱码
# （实测输出 `���ύ 84 ���ļ�`）。不依赖 PYTHONUTF8 环境变量，脚本自持。
for _stream in (sys.stderr,):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# 哨兵文件：项目根出现它，才允许自动提交
SENTINEL = ".dsh-autocommit.json"
# 可选的提交信息文件（用完即删）
MSG_FILE = ".dsh-autocommit-msg"
# 锁的陈旧阈值（秒）：超过它认为持锁进程已死，可抢占
LOCK_STALE_SEC = 300
# 提交信息里最多列多少个文件
MAX_FILES_IN_MSG = 30
# 单条 git 命令的超时（秒）
GIT_TIMEOUT = 60


def log(msg: str) -> None:
    """只写 stderr —— stdout 留给 hook 协议。"""
    print(f"[dsh-autocommit] {msg}", file=sys.stderr, flush=True)


def git(repo: Path, *args: str) -> tuple[int, str]:
    """跑一条 git 命令，返回 (returncode, stdout+stderr)。

    `core.quotepath=false` 是必需的：默认情况下 git 会把路径里的非 ASCII
    字符转义成 `\\346\\226\\207` 形式，中文文件名会以八进制串进入提交信息。
    """
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "未找到 git 可执行文件"
    except subprocess.TimeoutExpired:
        return 124, f"git {' '.join(args)} 超时"


def resolve_project_dir(payload: dict) -> Path:
    """定位「当前项目目录」。

    优先顺序与 hook 桥接的行为保持一致：
        CLAUDE_PROJECT_DIR 环境变量 → 载荷里的 cwd → 本进程工作目录
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env:
        return Path(env)
    cwd = str(payload.get("cwd") or "").strip()
    if cwd:
        return Path(cwd)
    return Path.cwd()


def acquire_lock(git_dir: Path):
    """在 .git 下抢排他锁。抢到返回文件对象，抢不到返回 None。

    陈旧锁会被抢占：持锁进程若已崩溃，锁文件会永远留着，
    那种情况下自动提交会静默失效——比报错更难发现，所以要主动清理。
    """
    lock = git_dir / "dsh-autocommit.lock"
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            return fd
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                return None
            if age < LOCK_STALE_SEC:
                return None
            log(f"发现陈旧锁（{age:.0f}s），抢占")
            try:
                lock.unlink()
            except OSError:
                return None
        except OSError as exc:
            log(f"无法创建锁：{exc}")
            return None
    return None


def summarize(changed: list[str]) -> str:
    """按变更文件的分布生成一句能看懂的摘要。

    不猜「这是修 bug 还是加功能」——猜错会让提交历史变得不可信。
    只如实描述「动了哪些区域、多少个文件」。
    """
    groups: dict[str, int] = {}
    for f in changed:
        top = f.split("/", 1)[0] if "/" in f else "(根目录)"
        groups[top] = groups.get(top, 0) + 1
    parts = [f"{k} {v}" for k, v in sorted(groups.items(),
                                           key=lambda kv: -kv[1])[:4]]
    return f"自动提交(回合结束): {len(changed)} 个文件 — " + "、".join(parts)


def build_message(repo: Path, changed: list[str]) -> str:
    """决定提交信息：优先用 Agent 指定的，否则自动生成。"""
    msg_path = repo / MSG_FILE
    if msg_path.is_file():
        try:
            text = msg_path.read_text(encoding="utf-8").strip()
            msg_path.unlink()
            if text:
                log(f"使用 {MSG_FILE} 指定的提交信息")
                return text
        except OSError as exc:
            log(f"读取 {MSG_FILE} 失败：{exc}")

    summary = summarize(changed)
    body = "\n".join(f"  {f}" for f in changed[:MAX_FILES_IN_MSG])
    if len(changed) > MAX_FILES_IN_MSG:
        body += f"\n  ...（其余 {len(changed) - MAX_FILES_IN_MSG} 个略）"
    return f"{summary}\n\n变更文件：\n{body}\n\n（由 DSH Stop hook 自动提交）"


def main() -> int:
    # ---- 读取载荷（读不到也不算错，Stop 不需要载荷内容） ----
    payload: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
    except (OSError, ValueError):
        pass

    project = resolve_project_dir(payload)

    # ---- 守卫 1：项目根必须有哨兵文件（显式 opt-in） ----
    if not (project / SENTINEL).is_file():
        return 0  # 静默跳过：绝大多数项目不该被自动提交

    # ---- 守卫 2：必须真的是一个 git 仓库，且根就是项目目录 ----
    if not project.is_dir():
        log(f"项目目录不存在：{project}")
        return 0
    code, top = git(project, "rev-parse", "--show-toplevel")
    if code != 0:
        log(f"不是 git 仓库，跳过：{project}")
        return 0
    git_root = Path(top.strip()).resolve()
    try:
        if git_root != project.resolve():
            log(f"git 根({git_root}) 与项目目录({project}) 不一致，跳过")
            return 0
    except OSError:
        return 0

    git_dir = git_root / ".git"
    if not git_dir.is_dir():
        log("未找到 .git 目录（worktree 暂不支持），跳过")
        return 0

    # ---- 守卫 3：变化为空就不提交（避免制造空提交） ----
    code, status = git(git_root, "status", "--porcelain")
    if code != 0:
        log(f"git status 失败：{status.strip()}")
        return 0
    changed = [ln[3:].strip() for ln in status.splitlines() if ln.strip()]
    if not changed:
        return 0  # 静默：没有变化是常态

    # ---- 并发保护 ----
    lock_fd = acquire_lock(git_dir)
    if lock_fd is None:
        log("另一个会话正在提交，本回合跳过")
        return 0

    try:
        # 抢到锁后重新看一眼（等锁期间可能已被别人提走）
        code, status = git(git_root, "status", "--porcelain")
        if code != 0 or not status.strip():
            return 0
        changed = [ln[3:].strip() for ln in status.splitlines() if ln.strip()]

        code, out = git(git_root, "add", "-A")
        if code != 0:
            log(f"git add 失败：{out.strip()}")
            return 0

        message = build_message(git_root, changed)
        code, out = git(git_root, "commit", "-m", message)
        if code != 0:
            log(f"git commit 失败：{out.strip()[:500]}")
            return 0

        code, head = git(git_root, "log", "-1", "--oneline")
        log(f"已提交 {len(changed)} 个文件：{head.strip()}")
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        try:
            (git_dir / "dsh-autocommit.lock").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 兜底：hook 崩溃绝不能影响回合
        log(f"未预期异常：{exc!r}")
        sys.exit(0)
