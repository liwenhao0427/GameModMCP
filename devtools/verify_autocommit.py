#!/usr/bin/env python3
"""验证 DSH Stop hook 自动提交脚本的行为。

为什么单测这个脚本
============================================================
它是**唯一会自动改动 git 历史**的东西。逻辑错了的后果不是「功能不好用」，
而是「把别的仓库提交了」或「制造大量空提交」——属于事故级。
所以它的守卫必须被自动验证，而不是靠人手工试。

覆盖的守卫
============================================================
    1. 无哨兵文件        → 静默跳过，不提交
    2. 有哨兵但不是仓库  → 跳过
    3. 哨兵 + 仓库 + 无变化 → 不制造空提交
    4. 哨兵 + 仓库 + 有变化 → 提交成功，且提交信息含变更文件
    5. 提交信息文件     → 被采用，且用后删除
    6. 非仓库目录       → 跳过（不抛异常）
    7. 并发抢锁         → 第二个进程跳过而非重复提交

所有场景都在 %TEMP% 下的一次性仓库里跑，**不碰真实项目仓库**。

    python devtools/verify_autocommit.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "hooks" / "auto_commit.py"
SENTINEL = ".dsh-autocommit.json"

PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    tag = "✅" if cond else "❌"
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    (PASS if cond else FAIL).append(label)


def show(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (p.stdout or "") + (p.stderr or "")


def run_hook(project: Path, payload: dict | None = None) -> tuple[int, str]:
    """在指定项目目录下跑 hook，返回 (exit code, stderr)。"""
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    p = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload or {}),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env=env, cwd=str(HERE.parent),
    )
    return p.returncode, p.stderr or ""


def new_repo(with_sentinel: bool, name: str) -> Path:
    """建一个一次性仓库。"""
    d = Path(tempfile.mkdtemp(prefix=f"acm-{name}-"))
    subprocess.run(["git", "-C", str(d), "init", "-q"], capture_output=True)
    git(d, "config", "user.name", "测试")
    git(d, "config", "user.email", "test@local")
    if with_sentinel:
        (d / SENTINEL).write_text("{}", encoding="utf-8")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "初始化")
    return d


def commit_count(repo: Path) -> int:
    out = git(repo, "rev-list", "--count", "HEAD")
    return int(out.strip()) if out.strip().isdigit() else 0


def touch(repo: Path, rel: str, text: str = "x") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


CLEANUP: list[Path] = []


def main() -> int:
    print("自动提交 hook 验证 —— 全部在临时仓库里跑，不碰真实项目")

    # ---------------- 1. 非仓库目录，无哨兵 ----------------
    show("1. 普通目录（无哨兵、无 git）→ 必须静默跳过")
    plain = Path(tempfile.mkdtemp(prefix="acm-plain-"))
    CLEANUP.append(plain)
    code, err = run_hook(plain)
    check("退出码为 0", code == 0, f"exit={code}")
    check("无任何输出（静默）", err.strip() == "", repr(err[:80]))

    # ---------------- 2. 有 git 但无哨兵 ----------------
    show("2. 是 git 仓库但**没有哨兵文件** → 必须跳过（防止误伤别的仓库）")
    repo_ns = new_repo(with_sentinel=False, name="nosentinel")
    CLEANUP.append(repo_ns)
    touch(repo_ns, "重要改动.txt")
    before = commit_count(repo_ns)
    code, err = run_hook(repo_ns)
    check("退出码为 0", code == 0, f"exit={code}")
    check("未产生提交", commit_count(repo_ns) == before,
          f"{before} → {commit_count(repo_ns)}")
    check("文件仍是未跟踪状态", "??" in git(repo_ns, "status", "--porcelain"))

    # ---------------- 3. 有哨兵但无变化 ----------------
    show("3. 有哨兵、但工作区无变化 → 不许制造空提交")
    repo_clean = new_repo(with_sentinel=True, name="clean")
    CLEANUP.append(repo_clean)
    before = commit_count(repo_clean)
    code, err = run_hook(repo_clean)
    check("退出码为 0", code == 0, f"exit={code}")
    check("提交数未增加（无空提交）", commit_count(repo_clean) == before,
          f"{before} → {commit_count(repo_clean)}")

    # ---------------- 4. 正常提交 ----------------
    show("4. 有哨兵 + 有变化 → 提交，且信息里列出变更文件")
    repo = new_repo(with_sentinel=True, name="normal")
    CLEANUP.append(repo)
    touch(repo, "src/gamemod_mcp/state.py", "a=1")
    touch(repo, "devtools/verify_x.py", "b=2")
    before = commit_count(repo)
    code, err = run_hook(repo)
    check("退出码为 0", code == 0, f"exit={code}")
    check("产生了 1 个提交", commit_count(repo) == before + 1,
          f"{before} → {commit_count(repo)}")
    msg = git(repo, "log", "-1", "--pretty=%B")
    check("提交信息含变更文件数", "2 个文件" in msg, msg.strip().splitlines()[0] if msg.strip() else "")
    check("提交信息含目录分布", "src" in msg and "devtools" in msg)
    check("工作区变干净", git(repo, "status", "--porcelain").strip() == "")
    check("stderr 日志为可读中文（非乱码）", "已提交" in err, repr(err.strip()[:90]))

    # ---------------- 5. 指定提交信息 ----------------
    show("5. 存在 .dsh-autocommit-msg → 采用它，且用后删除")
    repo_msg = new_repo(with_sentinel=True, name="msg")
    CLEANUP.append(repo_msg)
    touch(repo_msg, "a.txt")
    (repo_msg / ".dsh-autocommit-msg").write_text(
        "修复(runtime): ensure_runtime 早退导致指定 pid 被忽略", encoding="utf-8")
    code, err = run_hook(repo_msg)
    msg = git(repo_msg, "log", "-1", "--pretty=%s").strip()
    check("退出码为 0", code == 0, f"exit={code}")
    check("采用了指定信息", msg == "修复(runtime): ensure_runtime 早退导致指定 pid 被忽略", msg)
    check("信息文件已被删除", not (repo_msg / ".dsh-autocommit-msg").exists())

    # ---------------- 6. 删除文件也会被提交 ----------------
    show("6. 删除文件同样是变更 → 必须提交（用 git add -A 而非 add .）")
    repo_del = new_repo(with_sentinel=True, name="del")
    CLEANUP.append(repo_del)
    touch(repo_del, "to_be_removed.txt")
    run_hook(repo_del)
    (repo_del / "to_be_removed.txt").unlink()
    before = commit_count(repo_del)
    code, err = run_hook(repo_del)
    check("删除被提交", commit_count(repo_del) == before + 1,
          f"{before} → {commit_count(repo_del)}")
    check("确实是删除（D 记录）",
          "to_be_removed.txt" in git(repo_del, "log", "-1", "--name-status"))

    # ---------------- 7. 并发 ----------------
    show("7. 并发调用 → 不得重复提交（靠 .git 下的排他锁）")
    repo_par = new_repo(with_sentinel=True, name="par")
    CLEANUP.append(repo_par)
    touch(repo_par, "concurrent.txt")
    before = commit_count(repo_par)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(repo_par)
    procs = [subprocess.Popen(
        [sys.executable, str(SCRIPT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env, cwd=str(HERE.parent))
        for _ in range(4)]
    for p in procs:
        try:
            p.communicate(json.dumps({}), timeout=90)
        except subprocess.TimeoutExpired:
            p.kill()
    added = commit_count(repo_par) - before
    check("并发下只产生 1 个提交（未重复）", added == 1, f"新增提交数={added}")
    check("无残留锁文件", not (repo_par / ".git" / "dsh-autocommit.lock").exists())

    # ---------------- 汇总 ----------------
    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for d in CLEANUP:
            shutil.rmtree(d, ignore_errors=True)
    sys.exit(rc)
