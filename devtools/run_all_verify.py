"""一条命令跑完所有验收脚本，并在结束时给出动态覆盖率。

为什么需要这个（而不是在 PowerShell 里写个 foreach）：

· **区分「断言失败」与「进程崩溃」**。`verify_agent_js.py` 会把 Frida
  注入**自己的进程**来验证探针能被编译加载 —— 这在 Windows 无显示环境
  下偶发访问违例（exit 0xC0000005），脚本自己抓不住（进程直接死了）。
  崩了不等于产品有问题，但**也不能当成通过**，所以要单独归类并重试。
· **重试必须留痕**。允许重试是为了吸收环境抖动；如果重试了却不报告，
  就等于把偶发失败悄悄洗成通过。所以这里会明确打印哪些脚本重试过。
· **结束时跑覆盖率门禁**，把「工具是否真的被调用过」一起卡住。

用法：
    python devtools/run_all_verify.py              # 全部 + 覆盖率门禁
    python devtools/run_all_verify.py --no-cov     # 只跑验收脚本
    python devtools/run_all_verify.py --retries 2  # 崩溃时最多重试几次
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 已知会**自身崩溃**（而非断言失败）的脚本：Frida 自注入本进程，
# 无显示环境下偶发访问违例。给它们额外的重试预算。
SELF_ATTACH = {"verify_agent_js"}

# 退出码含义：0 通过；其余为失败。Windows 上被强杀/访问违例
# 会落在很大的负数或 1，这里统一按「崩溃」处理。
CRASH_CODES = {-1073741819, 3221225477, -1073740791, 3221225725}

COV_LOG = os.path.join(ROOT, ".cache-test", "tool_calls.jsonl")


def child_env() -> dict:
    """给子进程带上动态覆盖率垫片。

    必须由本脚本统一带上（而不是让使用者自己 export）：
    否则覆盖率门禁会读到**上一次**留下的陈旧日志，
    出现「脚本明明没跑到某工具，门禁却通过」的假通过 ——
    这正是本项目最忌讳的那类错误。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(HERE, "_cov")
    env["GAMEMOD_COV_OUT"] = COV_LOG
    return env


def run(script: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, script)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=ROOT, env=child_env(),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cov", action="store_true",
                    help="不跑动态覆盖率门禁")
    ap.add_argument("--retries", type=int, default=1,
                    help="崩溃时最多重试次数（默认 1）")
    args = ap.parse_args()

    scripts = sorted(
        f for f in os.listdir(HERE)
        if f.startswith("verify_") and f.endswith(".py")
        # 这个脚本是静态清单检查，跑它很快但不算「验收工具」；
        # 仍要跑，只是注意它不是动态证据。
        and f != "verify_tool_coverage.py"
    )

    print("=" * 74)
    print(f"验收脚本总览：{len(scripts)} 个")
    print("=" * 74)

    # 先清掉旧日志：覆盖率必须是**本次**跑出来的证据，
    # 不能与上一轮的结果混在一起。
    if not args.no_cov:
        try:
            os.remove(COV_LOG)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"⚠️  旧覆盖率日志删不掉（{exc}），结果可能混入上一次的数据")

    failed: list[str] = []
    crashed: list[str] = []
    retried: list[str] = []

    for name in scripts:
        stem = name[:-3]
        code, out = run(name)

        # 崩溃：重试。断言失败（正常非零退出）不重试 —— 那是确定的结论。
        # 自注入 Frida 的脚本（SELF_ATTACH）本身就是崩溃高发户，
        # 给它们**多一次**预算，但仍要留痕（重试过就记进 retried）。
        budget = args.retries + (1 if stem in SELF_ATTACH else 0)
        attempts = 0
        while code in CRASH_CODES and attempts < budget:
            attempts += 1
            print(f"[崩溃] {stem} exit={code}"
                  f"（{out.strip().splitlines()[-1][:60] if out.strip() else '无输出'}）"
                  f" —— 第 {attempts} 次重试…")
            time.sleep(1.0)
            code, out = run(name)

        if code == 0:
            mark = "✅"
            if attempts:
                retried.append(stem)
                mark = "✅(重试)"
            print(f"[{mark}] {stem}")
        elif code in CRASH_CODES:
            crashed.append(stem)
            print(f"[💥] {stem} exit={code} —— 进程崩溃（非断言失败）")
            for line in out.strip().splitlines()[-4:]:
                print(f"       {line}")
        else:
            failed.append(stem)
            print(f"[❌] {stem} exit={code}")
            for line in out.strip().splitlines()[-10:]:
                print(f"       {line}")

    print()
    print("=" * 74)
    print(f"通过 {len(scripts) - len(failed) - len(crashed)} / {len(scripts)}"
          f"    断言失败 {len(failed)}    进程崩溃 {len(crashed)}")
    if retried:
        print(f"重试后通过（环境抖动）：{', '.join(retried)}")
        print("  ⚠️  出现重试说明本机存在不稳定因素，结论比一次跑通要弱。")
    if failed:
        print(f"失败：{', '.join(failed)}")
    if crashed:
        print(f"崩溃：{', '.join(crashed)}")
        print("  崩溃多为 Frida 自注入本进程导致，非产品缺陷；")
        print("  但**不算通过**，需在有显示环境/管理员权限下复核。")
    print("=" * 74)

    rc = 1 if (failed or crashed) else 0

    if not args.no_cov:
        print()
        print("动态覆盖率门禁（devtools/report_tool_coverage.py --strict）")
        print("-" * 74)
        cov = subprocess.run(
            [sys.executable, os.path.join(HERE, "report_tool_coverage.py"),
             "--strict"],
            cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        )
        if cov.returncode != 0:
            print("覆盖率门禁未通过：有工具没被真正调用过。")
            rc = 1
        else:
            print("覆盖率门禁通过。")
        print("  注：覆盖率日志由本脚本统一设置（PYTHONPATH=devtools/_cov），")
        print("     已在开跑前清空，因此是本次运行的证据。")

    return rc


if __name__ == "__main__":
    sys.exit(main())
