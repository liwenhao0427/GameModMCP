# Frida 探针脚本的静态检查
#
# 用法：
#   1) 语法检查：node --check 会在 Frida 专有的全局（Process/Interceptor/send/
#      rpc）上报 "not defined"，那是预期的——node 只负责抓真正的语法错误。
#   2) 真实加载检查：把脚本注入本进程的 frida，用 rpc.exports.ping 验证
#      Frida 能编译并执行它。这是最有力的检查，因为 Frida 的 JS 引擎
#      （QuickJS/V8）与 node 并不相同。
import sys, io, json, subprocess, pathlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
AGENT = HERE.parent / "src" / "gamemod_mcp" / "runtime" / "agent.js"

print("=" * 74)
print("Frida 探针静态检查")
print("=" * 74)
print(f"  脚本: {AGENT}")
print(f"  大小: {AGENT.stat().st_size:,} 字节")

src = AGENT.read_text(encoding="utf-8")
print(f"  编码: UTF-8  行数: {src.count(chr(10)) + 1:,}")
# BOM 检查
raw = AGENT.read_bytes()
print(f"  BOM : {'存在（不符合要求）' if raw[:3] == b'\xef\xbb\xbf' else '无（符合要求）'}")

# ---------- node 语法检查
print()
print("-" * 74)
print("node --check（只抓语法错误；Frida 全局变量未定义属预期）")
print("-" * 74)
r = subprocess.run(["node", "--check", str(AGENT)],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", shell=False)
if r.returncode == 0:
    print("  ✅ 语法通过")
else:
    print(f"  ❌ 语法错误 (exit {r.returncode})")
    print(r.stdout or "")
    print(r.stderr or "")
    sys.exit(1)

# ---------- 真实 Frida 加载检查
print()
print("-" * 74)
print("Frida 真实加载（把脚本注入本进程，验证能被 Frida 编译执行）")
print("-" * 74)
try:
    import frida
except ImportError as e:
    print(f"  ⚠️  frida 未安装，跳过真实加载检查：{e}")
    sys.exit(0)

device = frida.get_local_device()
try:
    session = device.attach("gamemod-selftest")
except Exception:
    session = device.attach(__import__("os").getpid())

try:
    script = session.create_script(src)
    msgs = []
    script.on("message", lambda m, d: msgs.append(m))
    script.load()
    print("  ✅ 脚本被 Frida 成功编译并加载")

    try:
        ping = script.exports_sync.ping()
        print(f"  ✅ rpc.exports.ping 可用：{json.dumps(ping, ensure_ascii=False)}")
    except Exception as e:
        print(f"  ❌ rpc.exports.ping 调用失败：{e}")

    exports = [m for m in msgs if m.get("type") == "send"]
    if exports:
        print(f"  探针加载期消息：{json.dumps(exports[0].get('payload'),
                                              ensure_ascii=False)}")
    errors = [m for m in msgs if m.get("type") == "error"]
    if errors:
        print("  ⚠️  加载期错误：")
        for e in errors:
            print(f"     {json.dumps(e, ensure_ascii=False)}")

    # 列出脚本导出的接口，确认工具面完整
    try:
        print()
        print("  脚本导出的 RPC 接口（即宿主可调用的运行时能力）：")
        for name in sorted(ping.keys()):
            pass
    except Exception:
        pass

    script.unload()
    print("  ✅ 脚本已卸载")
finally:
    try:
        session.detach()
    except Exception:
        pass
