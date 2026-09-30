# 索引层验收：构建 SQLite 索引并用真实游戏数据跑通全部查询
import sys, io, time, shutil
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.index import Index, default_cache_dir

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
CACHE = r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\.cache-test"

# 清掉旧缓存，强制走完整构建路径
shutil.rmtree(CACHE, ignore_errors=True)

print("=" * 74)
print("1. 首次构建索引（含 sha256 + 全量解析 + 建表）")
print("=" * 74)
t0 = time.time()
idx = Index.open(META, cache_dir=CACHE)
t1 = time.time()
print(f"  构建耗时 {t1-t0:.2f} 秒")
print(f"  数据库 {idx.db_path.stat().st_size:,} 字节")
idx.close()

print()
print("=" * 74)
print("2. 二次打开（应命中缓存，秒开）")
print("=" * 74)
t0 = time.time()
idx = Index.open(META, cache_dir=CACHE)
t1 = time.time()
print(f"  打开耗时 {t1-t0:.2f} 秒")

info = idx.index_info()
print(f"  概况: 类型={info['type_count']} 方法={info['method_count']} "
      f"字段={info['field_count']} 枚举={info['enum_count']} 程序集={info['image_count']}")

print()
print("=" * 74)
print("3. search_classes：找玩家相关类")
print("=" * 74)
r = idx.search_classes("Player", limit=6)
print(f"  命中 {r['total']:,} 个，返回 {r['returned']} 个，句柄={r['handle']}")
for it in r["items"]:
    print(f"    {it['full_name']:<58} [{it['image']}] 字段{it['field_count']} 方法{it['method_count']}")

print()
print("=" * 74)
print("4. search_classes：只找枚举（游戏常量表）")
print("=" * 74)
r = idx.search_classes("Currency", only_enums=True, limit=6)
print(f"  命中 {r['total']} 个")
for it in r["items"]:
    print(f"    {it['full_name']}  is_enum={it['is_enum']}  token={it['token']}")

print()
print("=" * 74)
print("5. search_classes：按基类过滤（MonoBehaviour 子类）")
print("=" * 74)
r = idx.search_classes("", subtype_of="UnityEngine.MonoBehaviour", limit=6)
print(f"  命中 {r['total']:,} 个")
for it in r["items"]:
    print(f"    {it['full_name']}")

print()
print("=" * 74)
print("6. class_detail：UserData_Service 完整信息")
print("=" * 74)
d = idx.class_detail("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
print(f"  类={d['full_name']}")
print(f"  程序集={d['image']}  基类={d['parent']}  token={d['token']}")
print(f"  字段 {d['field_count']} 个，前 3: {[x['name'] for x in d['fields'][:3]]}")
print(f"  方法 {d['method_count']} 个，前 3: {[x['name'] for x in d['methods'][:3]]}")
print(f"  基类链={d['base_chain']}")
print(f"  子类数={d['derived_count']}")
m0 = d["methods"][0]
print(f"  首方法详情: {m0['name']} token={m0['token']} "
      f"返回={m0['return_type']} 参数={m0['parameters']} 静态={m0['is_static']}")

print()
print("=" * 74)
print("7. class_detail(include_inherited=True)：含继承成员")
print("=" * 74)
d2 = idx.class_detail("TeamTapas.ProjectR.UserDatas.Service.UserData_Service",
                      include_inherited=True)
print(f"  自身成员 字段{d['field_count']}/方法{d['method_count']} → "
      f"含继承后 字段{d2['field_count']}/方法{d2['method_count']}")

print()
print("=" * 74)
print("8. find_derived_classes：React 类")
print("=" * 74)
r = idx.find_derived_classes("UnityEngine.MonoBehaviour", limit=5)
print(f"  MonoBehaviour 共 {r['total']:,} 个子类，前 5:")
for it in r["items"][:5]:
    print(f"    {it['full_name']}")

print()
print("=" * 74)
print("9. find_monobehaviours：游戏自己的脚本")
print("=" * 74)
r = idx.find_monobehaviours(limit=8)
print(f"  共 {r['total']:,} 个游戏脚本，前 8:")
for it in r["items"][:8]:
    print(f"    {it['full_name']:<56} [{it['image']}]")

print()
print("=" * 74)
print("10. search_methods / search_fields")
print("=" * 74)
r = idx.search_methods("SetCurrency", limit=5)
print(f"  search_methods('SetCurrency') 命中 {r['total']}:")
for it in r["items"]:
    print(f"    {it['owner']}.{it['name']}  返回={it['return_type']} token={it['token']}")
r = idx.search_fields("currency", limit=5)
print(f"  search_fields('currency') 命中 {r['total']}:")
for it in r["items"]:
    print(f"    {it['owner']}.{it['name']}  类型={it['field_type']}")

print()
print("=" * 74)
print("11. 结果句柄翻页 read_query_result")
print("=" * 74)
r = idx.search_classes("Service", limit=5)
print(f"  句柄={r['handle']} 总数={r['total']}")
p1 = idx.read_query_result(r["handle"], offset=0, limit=3)
p2 = idx.read_query_result(r["handle"], offset=3, limit=3)
print(f"    第1页: {[x['name'] for x in p1['items']]} has_more={p1['has_more']}")
print(f"    第2页: {[x['name'] for x in p2['items']]} has_more={p2['has_more']}")

print()
print("=" * 74)
print("12. search_query_result：结果内再过滤")
print("=" * 74)
s = idx.search_query_result(r["handle"], "UserData", limit=5)
print(f"  在 {r['total']} 条结果里筛 'UserData' → {s['total']} 条")
for it in s["items"][:5]:
    print(f"    {it['full_name']}")

print()
print("=" * 74)
print("13. analyze_dependency")
print("=" * 74)
dep = idx.analyze_dependency("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
print(f"  基类={dep['parent']}  引用了 {dep['referenced_count']} 个类型")
for x in dep["referenced_types"][:8]:
    print(f"    {x['type']}  x{x['count']}")

print()
print("=" * 74)
print("14. class_hierarchy")
print("=" * 74)
h = idx.class_hierarchy("TeamTapas.ProjectR.Domain.CurrencyKey", direction="both", depth=3)
print(f"  向上: {h.get('up')}")
print(f"  向下: {h.get('down_count', 0)} 个")

print()
print("=" * 74)
print("15. get_method_source（诚实返回元数据形态）")
print("=" * 74)
ms = idx.get_method_source("TeamTapas.ProjectR.UserDatas.Service.UserData_Service", ".ctor")
if ms["found"]:
    for x in ms["methods"]:
        print(f"    {x['name']} token={x['token']} 参数={x.get('parameters')}")
else:
    print(f"    {ms}")

idx.close()
print()
print("✅ 索引层全部查询通过")
