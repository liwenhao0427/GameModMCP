"""验收：**人工调试前端与调用流水**（`debug_ui` / `debuglog`）。

这个前端是**旁路设施**，所以验收的重点不是「页面好不好看」，
而是三件会真出事的事：

1. **它不许拖累业务**。日志写不进去（磁盘满 / 只读）、轮转失败、
   路径是文件不是目录 —— 任何一种都不许让一个本来正常的工具失败。
   这正是 `cap.py` 那条「附加保护出问题不许把功能弄坏」的同款要求。
2. **它看到的工具必须是真工具**。前端不另存一份清单，所以这里要对账：
   前端列的 45 个工具 == MCP 协议 `tools/list` 给出的 45 个，
   连分组也必须与 `server.py` 的「工具分层」完全一致
   （有任何一个没被分组就要报出来，而不是悄悄归到别处）。
3. **它必须真能回溯 AI**。仅仅「页面能调通」是不够的 ——
   要证明**另一个进程**里跑的 stdio MCP 客户端（模拟 AI 客户端）
   的调用，会与前端（`source=ui`）落进**同一份**流水。
   第 3 节用真实的子进程 stdio 客户端来验，不是同进程假装。

每个关键断言都配**非空转反证**（把实现改回坏写法应当报红），
分别在第 1（吞异常 vs 不吞）、第 4（轮转 vs 不轮转）节。

不需要游戏、不需要注入；只起一个临时的 HTTP 实例（端口 0）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from gamemod_mcp import debuglog as dlog                   # noqa: E402
from gamemod_mcp import server as S                        # noqa: E402

PASS = 0
FAIL = 0
WARN = 0

#: 所有流水都写到临时目录，**绝不**碰用户家目录里的真流水
TMP = Path(tempfile.mkdtemp(prefix="gamemod_debugui_"))


def check(label: str, cond: bool, detail: str = "",
          warn_only: bool = False) -> bool:
    global PASS, FAIL, WARN
    if cond:
        print(f"  \u2705 {label}" + (f" \u2014 {detail}" if detail else ""))
        PASS += 1
        return True
    if warn_only:
        print(f"  \u26a0\ufe0f  {label}" + (f" \u2014 {detail}" if detail else ""))
        WARN += 1
        return False
    print(f"  \u274c {label}" + (f" \u2014 {detail}" if detail else ""))
    FAIL += 1
    return False


def section(title: str) -> None:
    print()
    print("-" * 78)
    print(title)
    print("-" * 78)


# ================================================================
# 1. 日志的健壮性：宁可不记，也不许弄坏业务
# ================================================================


def test_robustness() -> None:
    section("1. 流水不许拖累业务（记不上就不记，绝不外抛）")

    orig_path = os.environ.get("GAMEMOD_DEBUG_LOG_PATH")
    orig_flag = os.environ.get("GAMEMOD_DEBUG_LOG")
    os.environ["GAMEMOD_DEBUG_LOG"] = "1"
    dlog.set_enabled(True)

    try:
        # ① 路径指向一个**目录**：写入必然失败
        broken = TMP / "adir"
        broken.mkdir(exist_ok=True)
        os.environ["GAMEMOD_DEBUG_LOG_PATH"] = str(broken)
        raised = ""
        try:
            dlog.record("probe_tool", {"a": 1}, {"ok": True}, ok=True)
        except BaseException as exc:      # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        check("路径不可写（是目录）时 record 不抛异常", raised == "",
              f"raised={raised!r}")
        st = dlog.stats()
        check("拿不到文件时 stats 仍如实返回 exists=False",
              st.get("ok") is True and st.get("exists") is False,
              f"exists={st.get('exists')}")

        # ② 反证：把「吞异常」去掉，同样输入必须抛 —— 证明①不是空转
        #
        # 注意这里**必须**用一个真的会失败的写入：路径的父级是个**文件**
        # （不是目录），建目录必然失败。第一版把它写成「在目录下面建子目录」，
        # 那其实会**成功**（目录下建目录是合法的），于是反证自己空转了 ——
        # 反证本身写空转就等于没有反证。
        #
        # 只断言「抛了」，**不**挑具体异常类型：Windows 上这里给的是
        # FileExistsError（不是 NotADirectoryError），咬死类型会让这条
        # 反证在不同系统上假红 —— 而它要证明的只是「裸写会炸」。
        blocker = TMP / "blocker_file"
        blocker.write_text("not a dir", encoding="utf-8")
        naive_raised = ""
        try:
            (blocker / "sub" / "calls.jsonl").parent.mkdir(parents=True,
                                                          exist_ok=True)
        except BaseException as exc:      # noqa: BLE001
            naive_raised = type(exc).__name__
        check("反证：不兜异常的同款写入确实会抛（证明上一条非空转）",
              naive_raised != "",
              f"naive raised = {naive_raised or '（没抛，反证本身空转了）'}")

        # ④ 同一情形下，产品这条路径必须**照样不抛**
        os.environ["GAMEMOD_DEBUG_LOG_PATH"] = str(
            blocker / "sub" / "calls.jsonl")
        raised2 = ""
        try:
            dlog.record("probe_tool2", {"a": 1}, {"ok": True}, ok=True)
        except BaseException as exc:      # noqa: BLE001
            raised2 = f"{type(exc).__name__}: {exc}"
        check("同样的坏路径下 record 仍然不抛（与反证成对）",
              raised2 == "", f"raised={raised2!r}")

        # ③ 正常路径：能写能读
        good = TMP / "ok" / "calls.jsonl"
        os.environ["GAMEMOD_DEBUG_LOG_PATH"] = str(good)
        dlog.record("alpha", {"x": 1}, {"ok": True}, ok=True,
                    elapsed_ms=1.5)
        dlog.record("beta", {"y": 2}, {"ok": False, "error": "boom"},
                    ok=False, error="boom", elapsed_ms=2.5)
        check("正常路径写入后文件存在", good.is_file(),
              f"{good.stat().st_size if good.is_file() else 0} B")

        rd = dlog.read_records(limit=10)
        check("read_records 读回 2 条（最新在前）",
              rd["returned"] == 2 and rd["records"][0]["tool"] == "beta",
              f"returned={rd['returned']} first={rd['records'][0]['tool'] if rd['records'] else None}")

        # ④ 过滤三种都要真的起作用
        only_fail = dlog.read_records(limit=10, status="fail")
        check("status=fail 只回失败的", 
              only_fail["returned"] == 1
              and only_fail["records"][0]["tool"] == "beta",
              f"returned={only_fail['returned']}")
        only_alpha = dlog.read_records(limit=10, tool="alph")
        check("tool= 子串过滤真的生效",
              only_alpha["returned"] == 1
              and only_alpha["records"][0]["tool"] == "alpha",
              f"returned={only_alpha['returned']}")
        by_q = dlog.read_records(limit=10, q="boom")
        check("q= 能在错误文本里搜到",
              by_q["returned"] == 1 and by_q["records"][0]["tool"] == "beta",
              f"returned={by_q['returned']}")

        # ⑤ 默认开关：没设环境变量时必须**关**（调试设施不许默认占磁盘）
        os.environ.pop("GAMEMOD_DEBUG_LOG", None)
        dlog.set_enabled(False)
        check("默认情况下开关是关的（is_enabled=False）",
              dlog.is_enabled() is False, f"is_enabled={dlog.is_enabled()}")

        # ⑥ 超大参数/返回要被截断并**标注**，不许撑爆日志
        big = "x" * (dlog._MAX_FIELD_CHARS + 5000)
        dlog.set_enabled(True)
        os.environ["GAMEMOD_DEBUG_LOG_PATH"] = str(TMP / "big" / "calls.jsonl")
        dlog.record("bigtool", {"blob": big}, {"ok": True, "blob": big}, ok=True)
        one = dlog.read_records(limit=1)
        rec = one["records"][0] if one["records"] else {}
        check("超大参数/返回被截断且标 truncated=True",
              rec.get("truncated") is True,
              f"truncated={rec.get('truncated')} "
              f"args_bytes={rec.get('args_bytes')} result_bytes={rec.get('result_bytes')}")
        check("截断后单条记录大小在合理范围（< 40 KB）",
              (len(json.dumps(rec, ensure_ascii=False)) < 40000),
              f"{len(json.dumps(rec, ensure_ascii=False))} 字符")

        # ⑦ 坏行（多进程写串了 / 文件被截断）不许让读取整个失败
        bad = TMP / "bad" / "calls.jsonl"
        os.environ["GAMEMOD_DEBUG_LOG_PATH"] = str(bad)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text('{"tool": "good1", "ok": true}\n'
                       '这不是 JSON\n'
                       '{"tool": "good2", "ok": true}\n',
                       encoding="utf-8")
        r = dlog.read_records(limit=10)
        check("坏行被跳过、好行照样读出（返回 ok=True）",
              r["ok"] is True and r["returned"] == 2,
              f"returned={r['returned']}")

        # ⑧ clear 要如实报告删了什么
        cl = dlog.clear()
        check("clear 删掉文件并如实列出 removed",
              cl["ok"] is True and len(cl["removed"]) >= 1
              and not bad.exists(),
              f"removed={len(cl['removed'])} failed={cl['failed']}")
        check("清空后 exists=False（不是假装清空）",
              dlog.stats()["exists"] is False,
              f"exists={dlog.stats()['exists']}")
    finally:
        dlog.set_enabled(False)
        if orig_path is None:
            os.environ.pop("GAMEMOD_DEBUG_LOG_PATH", None)
        else:
            os.environ["GAMEMOD_DEBUG_LOG_PATH"] = orig_path
        if orig_flag is None:
            os.environ.pop("GAMEMOD_DEBUG_LOG", None)
        else:
            os.environ["GAMEMOD_DEBUG_LOG"] = orig_flag


# ================================================================
# 2. 工具目录：前端看到的必须是真工具
# ================================================================


def test_catalog() -> None:
    section("2. 工具目录与 MCP 协议、与 server 分层三者对账")
    from gamemod_mcp import debug_ui as UI

    snap = UI.tools_snapshot()
    check("tools_snapshot 成功", snap.get("ok") is True, str(snap.get("error", "")))
    names = [t["name"] for t in snap["tools"]]

    proto = S.mcp._tool_manager._tools
    check("前端工具数 == MCP 注册的工具数",
          len(names) == len(proto),
          f"前端 {len(names)} / 注册 {len(proto)}")
    check("两边工具名集合完全相同（不重不漏）",
          set(names) == set(proto),
          f"仅前端有={sorted(set(names) - set(proto))[:5]} "
          f"仅注册有={sorted(set(proto) - set(names))[:5]}")

    check("没有任何工具落到「未分组」",
          snap["ungrouped"] == [],
          f"未分组={snap['ungrouped']}")
    check("分组数与 server.py 的「工具分层」一致（8 层）",
          len(snap["groups"]) == 8, f"groups={snap['groups']}")

    # 分组必须与 docstring 里写的逐字一致（不是「有个分组就行」）
    groups = UI.tool_groups()
    check("tool_groups 解析出全部 45 个工具（无遗漏）",
          len(groups) == len(names),
          f"解析出 {len(groups)} / 应有 {len(names)}；"
          f"缺={sorted(set(names) - set(groups))[:6]}")
    spot = {n: groups.get(n) for n in
            ("open_game", "search_classes", "hook_method", "ocr",
             "ue_profile", "get_diagnostics", "read_query_result",
             "find_game_processes")}
    expected = {
        "find_game_processes": "发现层", "open_game": "会话层",
        "search_classes": "静态查询层", "read_query_result": "结果句柄层",
        "hook_method": "运行时层", "ocr": "弱视觉/UI层",
        "ue_profile": "UE 层", "get_diagnostics": "诊断层",
    }
    check("抽查 8 个工具的分组全部正确",
          spot == expected, f"实际={spot}")

    # 参数表必须真的来自 schema
    by_name = {t["name"]: t for t in snap["tools"]}
    og = by_name["open_game"]
    check("open_game 的参数名与 schema 一致",
          {p["name"] for p in og["params"]} ==
          {"game_dir", "metadata_path", "engine", "cache_dir", "force_rebuild"},
          f"params={[p['name'] for p in og['params']]}")
    fr = [p for p in og["params"] if p["name"] == "force_rebuild"][0]
    check("force_rebuild 被识别为 boolean 且有默认值 False",
          fr["type"] == "boolean" and fr["default"] is False,
          f"type={fr['type']} default={fr['default']}")

    # 语义标注要如实透传（前端靠它标「只读/破坏性」）
    rd = by_name["search_classes"]
    hook = by_name["hook_method"]
    check("只读工具被标 read_only=True、非破坏",
          rd["read_only"] is True and rd["destructive"] is False,
          f"ro={rd['read_only']} ds={rd['destructive']}")
    check("hook_method 被标为会改动且接触外部世界",
          hook["read_only"] is False and hook["open_world"] is True,
          f"ro={hook['read_only']} ow={hook['open_world']}")


# ================================================================
# 3. 端到端：HTTP 接口 + **跨进程** AI 调用都进同一份流水
# ================================================================


def _http(base: str, path: str, method: str = "GET", body: dict | None = None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:                 # noqa: BLE001
            return exc.code, {"ok": False, "error": "非 JSON 错误体"}


def test_http_and_cross_process() -> None:
    section("3. 前端 HTTP 接口 + 跨进程 AI 客户端落进同一份流水")
    from gamemod_mcp import debug_ui as UI

    logfile = TMP / "e2e" / "calls.jsonl"
    os.environ["GAMEMOD_DEBUG_LOG"] = "1"
    os.environ["GAMEMOD_DEBUG_LOG_PATH"] = str(logfile)
    dlog.set_enabled(True)

    httpd = UI.make_server("127.0.0.1", 0)
    port = httpd.server_port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        # ---- 静态页与接口
        with urllib.request.urlopen(base + "/", timeout=30) as resp:
            html = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
        check("GET / 返回 HTML（含 UTF-8 声明）",
              resp.status == 200 and "text/html" in ctype
              and "charset=utf-8" in ctype,
              f"status={resp.status} type={ctype}")
        check("HTML 里引用了自己的接口（不是空壳）",
              "/api/tools" in html and "/api/call" in html,
              f"{len(html)} 字符")

        # 界面上两个**曾经写错**的地方，用 HTML/JS 层面的断言钉住。
        # 它们不是「好看不好看」，而是会让人看错信息：
        # ① 过滤工具时旧写法逐个 `display:none`，分组表头的计数不跟着变
        #    （实测截图里出现过「UE 层（5）」而其下一条都不可见）；
        # ② 没有深链接时，刷新会丢掉「我在看哪个工具」，自动化截图也只能
        #    靠像素坐标去点（很脆）。
        check("过滤工具走的是整表重渲染（分组计数不会与实际可见项脱节）",
              "buildList()" in html and "FILTER" in html,
              "buildList/FILTER 存在")
        check("支持深链接 #tool=<名字> 与 #tab=log（刷新/分享不丢视图）",
              "#tool=" in html and "applyHash" in html and "#tab=log" in html,
              "applyHash 存在")

        sc, snap = _http(base, "/api/tools")
        check("GET /api/tools 返回 45 个工具",
              sc == 200 and snap.get("count") == 45,
              f"status={sc} count={snap.get('count')}")

        sc, ses = _http(base, "/api/session")
        check("GET /api/session 可用", sc == 200 and ses.get("ok") is True,
              f"status={sc}")

        # ---- 真实调用：正路径 + 负路径
        sc, r1 = _http(base, "/api/call", "POST",
                       {"tool": "get_session", "args": {}})
        check("POST /api/call 真实调用成功且带回耗时",
              sc == 200 and r1.get("ok") is True
              and r1.get("result_ok") is True
              and isinstance(r1.get("elapsed_ms"), (int, float)),
              f"status={sc} result_ok={r1.get('result_ok')} "
              f"elapsed={r1.get('elapsed_ms')}ms bytes={r1.get('bytes')}")

        sc, r2 = _http(base, "/api/call", "POST",
                       {"tool": "inspect_binary",
                        "args": {"path": "C:\\不存在_xyz.exe"}})
        check("工具的**业务失败**被如实带回（ok=false + error）",
              r2.get("ok") is True and r2.get("result_ok") is False
              and "不存在" in str(r2.get("error", "")),
              f"result_ok={r2.get('result_ok')} error={r2.get('error')}")

        # 类型强转 + 类型错误必须报错
        sc, r3 = _http(base, "/api/call", "POST",
                       {"tool": "find_game_processes",
                        "args": {"only_games": "false"}})
        check("字符串 'false' 被正确转成布尔",
              r3.get("args", {}).get("only_games") is False,
              f"args={r3.get('args')}")
        sc, r4 = _http(base, "/api/call", "POST",
                       {"tool": "find_game_processes",
                        "args": {"only_games": "yesplease"}})
        check("类型转不动时如实报错，不许静默当默认值",
              r4.get("ok") is False and "布尔" in str(r4.get("error", "")),
              f"error={r4.get('error')}")
        sc, r5 = _http(base, "/api/call", "POST",
                       {"tool": "nope_tool", "args": {}})
        check("不存在的工具返回 ok=false",
              r5.get("ok") is False, f"error={r5.get('error')}")

        # ---- 开关与清理
        sc, on = _http(base, "/api/log/enabled", "POST", {"enabled": False})
        check("POST /api/log/enabled 能关掉记录",
              on.get("ok") is True and on.get("enabled") is False,
              f"enabled={on.get('enabled')}")
        before = len(dlog.read_records(limit=500)["records"])
        _http(base, "/api/call", "POST", {"tool": "get_session", "args": {}})
        after = len(dlog.read_records(limit=500)["records"])
        check("关掉之后调用**不再**进流水（开关真起作用）",
              after == before, f"关前 {before} 条 / 关后 {after} 条")
        sc, on2 = _http(base, "/api/log/enabled", "POST", {"enabled": True})
        check("能再打开", on2.get("enabled") is True)

        # ---- 跨进程：模拟 AI 客户端
        from mcp_stdio import McpStdioServer, script_form_env
        env = script_form_env(str(samples.SRC), clean_pythonpath=False)
        env["GAMEMOD_DEBUG_LOG"] = "1"
        env["GAMEMOD_DEBUG_LOG_PATH"] = str(logfile)
        srv = McpStdioServer([sys.executable, "-m", "gamemod_mcp.server"],
                             env=env, cwd=str(samples.ROOT)).start()
        try:
            init = srv.initialize()
            tl = srv.tools()
            check("另一个进程的 stdio MCP 客户端能握手（模拟 AI 客户端）",
                  init["ok"] is True and tl["ok"] is True,
                  f"init={init['ok']} tools={len(tl['names'])} err={init['error'][:120]}")
            srv.call_tool("get_session", {})
            srv.call_tool("search_methods",
                          {"pattern": "get_", "limit": 1})
            check("该客户端的 stdout 没有被污染（0 行非 JSON）",
                  srv.non_json_lines == 0, f"{srv.non_json_lines} 行")
        finally:
            srv.close()

        # 跨进程的记录必须真的落进了**同一份**文件
        recs = dlog.read_records(limit=500)["records"]
        sources = {r.get("source") for r in recs}
        pids = {r.get("pid") for r in recs}
        mine = os.getpid()
        check("流水里同时有 ui 与 mcp 两种来源（前端 + AI 客户端）",
              {"ui", "mcp"} <= sources, f"sources={sorted(sources)}")
        check("流水里含**另一个进程**的 pid（确实是跨进程落盘，不是同进程假装）",
              any(p != mine for p in pids),
              f"pids={sorted(pids)} 本进程={mine}")
        ai = [r for r in recs if r.get("source") == "mcp"]
        check("AI 客户端的调用记录带工具名与返回",
              len(ai) >= 2
              and all(r.get("tool") for r in ai)
              and any(r.get("result") for r in ai),
              f"mcp 记录 {len(ai)} 条，工具="
              f"{[r.get('tool') for r in ai][:5]}")

        # ---- 日志接口
        sc, stat = _http(base, "/api/log/stats")
        check("GET /api/log/stats 反映真实体积",
              stat.get("ok") is True and stat.get("enabled") is True
              and stat.get("current_bytes", 0) > 0,
              f"bytes={stat.get('current_bytes')}")
        sc, summ = _http(base, "/api/log/summary")
        check("GET /api/log/summary 汇总了两种来源",
              summ.get("ok") is True and summ.get("total", 0) >= 2
              and {"ui", "mcp"} <= set(summ.get("by_source", {})),
              f"total={summ.get('total')} by_source={summ.get('by_source')}")
        sc, page = _http(base, "/api/log/records?limit=3")
        check("GET /api/log/records 支持 limit",
              page.get("ok") is True and page.get("returned") <= 3
              and page.get("total_matched", 0) >= 2,
              f"returned={page.get('returned')} matched={page.get('total_matched')}")

        sc, cleared = _http(base, "/api/log/clear", "POST", {})
        check("POST /api/log/clear 清空并如实报告",
              cleared.get("ok") is True and cleared.get("removed"),
              f"removed={len(cleared.get('removed') or [])}")
        check("清空后 logfile 真的没了",
              not logfile.exists(), f"exists={logfile.exists()}")

        # ---- 404 与坏 JSON 不许把服务器弄崩
        # 注意：URL 里**不能**放非 ASCII（http.client 会按 ascii 编码请求行，
        # 直接 UnicodeEncodeError —— 那是测试自己的 bug，不是产品的）。
        try:
            urllib.request.urlopen(base + "/api/no_such_path", timeout=20)
            code = 200
        except urllib.error.HTTPError as exc:
            code = exc.code
        check("未知路径返回 404（不是崩掉连接）", code == 404, f"code={code}")
        req = urllib.request.Request(base + "/api/call", method="POST",
                                     data=b"{not json",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                code2, bad_body = resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code2, bad_body = exc.code, json.loads(exc.read().decode("utf-8"))
        check("请求体不是 JSON 时如实报错（400 + 说清是 JSON 坏了）",
              code2 == 400 and bad_body.get("ok") is False
              and "JSON" in str(bad_body.get("error", "")),
              f"code={code2} body={bad_body}")
        check("坏请求之后服务器仍活着（不是崩了连接）",
              _http(base, "/api/log/stats")[0] == 200,
              "后续请求正常")
    finally:
        httpd.shutdown()
        httpd.server_close()
        dlog.set_enabled(False)


# ================================================================
# 4. 轮转：不许无界增长
# ================================================================


def test_rotation() -> None:
    section("4. 流水文件轮转（不许把用户磁盘写满）")
    os.environ["GAMEMOD_DEBUG_LOG"] = "1"
    os.environ["GAMEMOD_DEBUG_LOG_MAX_BYTES"] = "4096"
    os.environ["GAMEMOD_DEBUG_LOG_MAX_FILES"] = "3"
    path = TMP / "rot" / "calls.jsonl"
    os.environ["GAMEMOD_DEBUG_LOG_PATH"] = str(path)
    dlog.set_enabled(True)
    try:
        for i in range(300):
            dlog.record(f"tool{i}", {"i": i, "pad": "z" * 100},
                        {"ok": True, "i": i}, ok=True)
        files = sorted(p.name for p in path.parent.glob("calls.jsonl*"))
        size = path.stat().st_size if path.exists() else 0
        check("写满后会轮转出多个文件",
              len(files) >= 2,
              f"files={files}")
        check("份数不超过 max_files=3",
              len(files) <= 3, f"files={files}")
        check(f"当前份不超过上限（{size} B < 4096 B 的合理倍数）",
              size <= 4096 * 2 + 2000, f"{size} B")

        # 总数守恒：轮转不该把文件删到一个不剩，也不该只留 1 份
        recs = dlog.read_records(limit=2000)
        check("轮转后仍能读到记录（不是把日志写没了）",
              recs["returned"] > 0, f"returned={recs['returned']}")

        # 反证：如果**不**轮转，同一批数据会全部堆在一个文件里
        naive = TMP / "naive" / "calls.jsonl"
        naive.parent.mkdir(parents=True, exist_ok=True)
        for i in range(300):
            with open(naive, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tool": f"tool{i}",
                                     "pad": "z" * 100}) + "\n")
        check("反证：不轮转时单文件会长到远超上限（证明上一条非空转）",
              naive.stat().st_size > 4096 * 3,
              f"naive={naive.stat().st_size} B vs 上限 4096 B")
    finally:
        dlog.set_enabled(False)
        os.environ.pop("GAMEMOD_DEBUG_LOG_MAX_BYTES", None)
        os.environ.pop("GAMEMOD_DEBUG_LOG_MAX_FILES", None)


# ================================================================
# 5. 与 MCP 业务隔离：它不许改变工具本身
# ================================================================


def test_isolation() -> None:
    section("5. 旁路隔离：不改工具实现、不污染 stdout、默认不写盘")
    from gamemod_mcp import debug_ui as UI   # noqa: F401

    check("server 模块里 45 个工具名仍是普通可同步调用的函数",
          all(callable(getattr(S, n, None))
              for n in ("open_game", "search_classes", "hook_method")),
          "S.open_game 等仍是函数（verify 脚本的 64 处同步调用不受影响）")

    check("RAW_TOOLS 仍是 45 个（体积上限那一层没被绕过）",
          len(S.RAW_TOOLS) == 45, f"{len(S.RAW_TOOLS)}")

    # 流水默认关闭 → 不碰磁盘
    orig = os.environ.pop("GAMEMOD_DEBUG_LOG", None)
    dlog.set_enabled(False)
    target = TMP / "should_not_exist" / "calls.jsonl"
    os.environ["GAMEMOD_DEBUG_LOG_PATH"] = str(target)
    dlog.record("anything", {}, {"ok": True}, ok=True)
    check("开关关闭时**一个字节都不写**（默认不占磁盘）",
          not target.exists(), f"exists={target.exists()}")
    if orig is not None:
        os.environ["GAMEMOD_DEBUG_LOG"] = orig

    check("stdio 入口没有被改动（main 仍走 stdio）",
          "stdio" in (S.main.__doc__ or ""), f"{S.main.__doc__!r}")

    # 调用钩子必须装在分发层（否则回溯不到 AI 客户端）
    check("调用流水钩子已装在 ToolManager.call_tool（分发层）",
          getattr(S._install_call_logger, "__call__", None) is not None
          and S._CALL_LOGGER is True,
          f"_CALL_LOGGER={S._CALL_LOGGER}")

    # 这条钉住的是**设计理由本身**，不是某个行为：
    # 记录点若放在「工具函数外层」或「前端里」，就只看得见经它自己走的调用。
    # 实测：绕开分发层直接调工具函数时**不会**被记录 —— 正说明记录点
    # 确实在分发层。若哪天有人把钩子改挂到工具函数上，这条会连同
    # 上面那条一起变化，设计意图不会悄悄丢掉。
    path = TMP / "dispatch_only" / "calls.jsonl"
    os.environ["GAMEMOD_DEBUG_LOG"] = "1"
    os.environ["GAMEMOD_DEBUG_LOG_PATH"] = str(path)
    dlog.set_enabled(True)
    S.get_session()                      # 直接调函数：不走分发层
    direct = dlog.read_records(limit=20)
    check("直接调工具函数（绕开分发层）不会被记录 —— 记录点确实在分发层",
          direct["returned"] == 0,
          f"returned={direct['returned']}（应为 0；>0 说明记录点不在分发层）")
    dlog.set_enabled(False)


def main() -> int:
    print("=" * 78)
    print("人工调试前端与调用流水（debug_ui / debuglog）")
    print(f"临时目录：{TMP}")
    print("=" * 78)
    test_robustness()
    test_catalog()
    test_http_and_cross_process()
    test_rotation()
    test_isolation()
    print()
    print("=" * 78)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
