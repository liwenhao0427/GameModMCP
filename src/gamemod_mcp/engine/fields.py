"""字段宽度 / 编码 / 解释：`read_field_value` 与 `set_field_value` 的共用底层。

为什么单独成模块
============================================================
这些是**纯函数**（给定类型名与值就能算出字节），与 MCP 工具编排无关。
它们原先堆在 `server.py` 里，把一个「只做参数校验 → 调下层 → 组装返回」的
编排文件撑到 4,000 行以上。按 AGENTS.md §3 的分层规则，这类逻辑属于
`engine/`。

设计约束（改动时务必遵守）
============================================================
· 本模块**不 import** `server.py` —— 依赖方向只能是 server → engine，
  反向 import 会形成循环且让分层失去意义。
· 这里只放**不组装工具响应**的东西。`_static_field_hit` /
  `_read_static_field` / `_write_static_field` 要用 `_err` / `_ok` 组装返回，
  属于编排，仍留在 `server.py`。
· 宽度表查不到时按 8 字节（指针宽度）—— 见 `_field_read_width` 的说明，
  这是**故意的保守默认**，不是遗漏。
"""

from __future__ import annotations

import struct
from typing import Any

#: 字段类型名 → 读取宽度（字节）。
#:
#: 为什么需要它：`read_field_value` 原来**固定读 8 字节**。字段只有 4 字节时，
#: 那 4 个「多读的」字节是**相邻字段**的内容 —— 既会给出无意义的
#: `as_int64`（两个字段拼出来的数），又会在字段贴近页尾时把一次**本可成功**
#: 的读取变成假失败。宽度按字段类型来，才不会读到别人的字节。
#:
#: 键同时收 IL2CPP 的 `il2cpp_type_get_name` 形式（`System.Int32`）与短名，
#: 因为运行时返回的是前者；查不到就按 8 字节（**引用类型/指针**在 x64 上
#: 就是 8 字节，且这是原来的行为，不会把没覆盖的类型改坏）。
_FIELD_WIDTH: dict[str, int] = {
    "System.Boolean": 1, "bool": 1,
    "System.Byte": 1, "byte": 1, "System.SByte": 1, "sbyte": 1,
    "System.Char": 2, "char": 2,
    "System.Int16": 2, "short": 2, "System.UInt16": 2, "ushort": 2,
    "System.Int32": 4, "int": 4, "System.UInt32": 4, "uint": 4,
    "System.Single": 4, "float": 4,
    "System.Int64": 8, "long": 8, "System.UInt64": 8, "ulong": 8,
    "System.Double": 8, "double": 8, "System.IntPtr": 8, "System.UIntPtr": 8,
}


#: 字段类型名 → 写值时的「解释方式」。与 `_FIELD_WIDTH` 同一张表的口径，
#: 但**必须区分有符号/无符号/浮点**：同样 4 字节，写成 int 还是 float
#: 得到的字节完全不同，而 `as_int32` 与 `as_float` 在读取时会同时给出，
#: 写的时候却只能选一个 —— 选错的后果是「值看着变了、游戏行为不对」。
_FIELD_WRITE_KIND: dict[str, str] = {
    "System.Boolean": "bool", "bool": "bool",
    "System.SByte": "int", "sbyte": "int",
    "System.Byte": "uint", "byte": "uint",
    "System.Int16": "int", "short": "int",
    "System.UInt16": "uint", "ushort": "uint",
    "System.Int32": "int", "int": "int",
    "System.UInt32": "uint", "uint": "uint",
    "System.Int64": "long", "long": "long",
    "System.UInt64": "ulong", "ulong": "ulong",
    "System.Single": "float", "float": "float",
    "System.Double": "double", "double": "double",
    "System.Char": "uint", "char": "uint",
    "System.IntPtr": "long", "System.UIntPtr": "ulong",
}


#: 枚举没有显式底层类型时的默认宽度来源。
#:
#: C# 枚举的默认底层类型就是 `int`（System.Int32，4 字节）——
#: 这是语言规范定的，不是我们猜的。显式声明 `: byte` / `: long` 的枚举
#: 由探针的 `enum_base_type` 给出真值，走不到这个默认值。
_ENUM_DEFAULT_BASE = "System.Int32"


def _field_read_width(type_name: Any) -> int:
    """字段类型名 → 要读几个字节。查不到按 8（指针宽度，也是原来的行为）。"""
    if not type_name:
        return 8
    return _FIELD_WIDTH.get(str(type_name).strip(), 8)


def _resolve_enum_underlying(st: Any, ftype: Any, hit: dict) -> str | None:
    """字段如果是**枚举**，返回它的底层整型类型名；否则返回 `None`。

    为什么需要它：字段枚举 API 只给类型名字符串，**枚举字段给的是枚举类名**
    （如 `EasingType`），与普通类名长得一模一样。于是 `_field_read_width`
    查不到表，按默认的 8 字节读 —— 4 字节的枚举会把**相邻字段**的 4 字节
    一起读进来（`as_int64` 是两个字段拼出来的数）；`set_field_value` 更糟，
    它会因为「宽度 8 且类型名认不出」把枚举误判成**引用类型**而直接拒绝写入。

    两条来源，按可信度排序：

      1. **运行时探针**（`hit["is_enum"]`）—— 最可靠，直接问过
         `il2cpp_class_is_enum` / `mono_class_is_enum`，且能给出显式底层类型
         （`enum_base_type`）。只有探针版本支持这几个导出时才有这个键。
      2. **静态索引**（`is_enum` 列）—— 探针给不出判定时兜底。
         按 `nested_name` / `full_name` / `name` 三写法查（与
         `_match_type_ids` 同口径，嵌套类型必须用完整名）。

    两条都拿不到时返回 `None` —— **不猜**。猜错的代价是「按 8 字节读一个
    4 字节字段」，那正是这个函数要修的缺陷本身。
    """
    # ① 运行时探针的判定（首选）
    if hit.get("is_enum") is True:
        base = hit.get("enum_base_type")
        return str(base).strip() if base else _ENUM_DEFAULT_BASE
    if hit.get("is_enum") is False:
        return None

    # ② 静态索引兜底（探针没给 is_enum 键时）
    name = str(ftype).strip() if ftype else ""
    if not name:
        return None
    if name in _FIELD_WIDTH:
        return None                       # 基础类型，本来就不需要兜底
    try:
        if not st.has_index():
            return None
        idx = st.index
        if idx is None:
            return None
        row = idx.conn.execute(
            "SELECT is_enum FROM types "
            "WHERE nested_name = ? OR full_name = ? OR name = ? "
            "ORDER BY (nested_name = ?) DESC, (full_name = ?) DESC LIMIT 1",
            (name, name, name, name, name),
        ).fetchone()
    except Exception:  # noqa: BLE001 - 兜底失败不该让读写字段整个失败
        return None
    if row and row["is_enum"]:
        return _ENUM_DEFAULT_BASE
    return None


def _field_write_kind(field_type: Any, width: int) -> str:
    """字段类型 + 宽度 → 写值方式。

    类型认不出来时**按宽度**给一个保底解释：4 字节按 int、8 字节按 long。
    为什么不按 ptr：写指针是极危险的操作，而「引用类型字段」写指针
    在语义上多半是错的（用户想要的是改内容而不是换对象），
    所以引用类型这里**统一报错**，让调用方改用 `eval_script` 或 Hook。
    """
    key = str(field_type).strip() if field_type else ""
    if key in _FIELD_WRITE_KIND:
        return _FIELD_WRITE_KIND[key]
    if width == 1:
        return "uint"
    if width == 2:
        return "int"
    if width == 4:
        return "int"
    if width == 8:
        return "long"
    return "bytes"


def _encode_field_value(value: Any, width: int, kind: str) -> tuple[bytes | None, str]:
    """把调用方给的值编码成 `width` 字节。返回 `(bytes, error)`。

    抽成**纯函数**是为了能在不启动游戏的情况下验收（§5 要求纯函数与
    端到端两层都验）：编码错了会让「写入成功但值不对」，而那种缺陷
    在 e2e 里很难与「游戏自己覆盖了」区分开。

    规矩：**溢出直接报错，绝不静默截断**。把 300 写进 1 字节字段如果
    截成 44，调用方会看到「ok:true」却拿到一个自己没要的值。
    """
    import struct as _struct

    def _need(cond: bool, msg: str) -> str | None:
        return None if cond else msg

    try:
        if kind == "bytes":
            s = str(value).strip()
            if s.startswith("0x") or s.startswith("0X"):
                s = s[2:]
            s = s.replace(" ", "")
            if len(s) % 2:
                return None, f"字节串长度必须是偶数个十六进制字符：{value!r}"
            try:
                b = bytes.fromhex(s)
            except ValueError:
                return None, f"不是合法的十六进制字节串：{value!r}"
            if len(b) != width:
                return None, (f"字节串长度 {len(b)} 与字段宽度 {width} 不一致"
                              f"（字段类型决定宽度，不能多也不能少）")
            return b, ""

        if kind == "bool":
            if isinstance(value, bool):
                return (b"\x01" if value else b"\x00"), ""
            s = str(value).strip().lower()
            if s in ("1", "true", "yes", "on"):
                return b"\x01", ""
            if s in ("0", "false", "no", "off"):
                return b"\x00", ""
            return None, f"bool 字段只认 true/false/1/0，收到 {value!r}"

        if kind == "float":
            return _struct.pack("<f", float(value)), ""
        if kind == "double":
            return _struct.pack("<d", float(value)), ""

        # 整数族：先按字符串解析，避免 float 传进来被静默截断
        if isinstance(value, bool):
            return None, f"bool 不能写进 {kind} 字段（要写 1/0 请写整数）"
        s = str(value).strip()
        try:
            iv = int(s, 0)  # 支持 0x 前缀
        except ValueError:
            return None, f"{kind} 字段要整数，收到 {value!r}"

        limits = {
            "int": (-(1 << 7), (1 << 7) - 1, "b", 1),
            "uint": (0, (1 << 8) - 1, "B", 1),
            "long": (-(1 << 63), (1 << 63) - 1, "q", 8),
            "ulong": (0, (1 << 64) - 1, "Q", 8),
        }
        # 1/2/4 字节统一走 int/uint 的宽度表
        width_limits = {
            (1, "int"): (-128, 127), (1, "uint"): (0, 255),
            (2, "int"): (-32768, 32767), (2, "uint"): (0, 65535),
            (4, "int"): (-(1 << 31), (1 << 31) - 1),
            (4, "uint"): (0, (1 << 32) - 1),
        }
        if kind in ("int", "uint") and (width, kind) in width_limits:
            lo, hi = width_limits[(width, kind)]
        elif kind in limits:
            lo, hi = limits[kind][0], limits[kind][1]
        else:
            return None, f"不支持的值类型：{kind}"
        if not (lo <= iv <= hi):
            return None, (f"{iv} 超出 {kind}({width} 字节) 的范围 [{lo}, {hi}]；"
                          f"溢出不会被静默截断")
        fmt = {1: ("b" if kind == "int" else "B"),
               2: ("h" if kind == "int" else "H"),
               4: ("i" if kind == "int" else "I"),
               8: ("q" if kind in ("long", "int") else "Q")}[width]
        return _struct.pack("<" + fmt, iv), ""
    except (TypeError, ValueError, OverflowError) as exc:
        return None, f"编码失败：{exc}"


def _explain_bytes(hexs: str) -> dict:
    """按**实际长度**解释一段小端字节，只给这个长度下有意义的解释。

    与 `read_field_value` 用同一口径（那边是内联的），抽出来给
    `set_field_value` 复用，避免两处对同一段字节给出不同解释。
    """
    import struct as _struct
    out: dict[str, Any] = {}
    try:
        if len(hexs) >= 2:
            b1 = bytes.fromhex(hexs[:2])
            out["as_int8"] = _struct.unpack("<b", b1)[0]
            out["as_uint8"] = _struct.unpack("<B", b1)[0]
            out["as_bool"] = b1[0] != 0
        if len(hexs) >= 4:
            b2 = bytes.fromhex(hexs[:4])
            out["as_int16"] = _struct.unpack("<h", b2)[0]
            out["as_uint16"] = _struct.unpack("<H", b2)[0]
        if len(hexs) >= 8:
            b4 = bytes.fromhex(hexs[:8])
            out["as_int32"] = _struct.unpack("<i", b4)[0]
            out["as_uint32"] = _struct.unpack("<I", b4)[0]
            out["as_float"] = round(_struct.unpack("<f", b4)[0], 6)
        if len(hexs) >= 16:
            b8 = bytes.fromhex(hexs[:16])
            out["as_int64"] = _struct.unpack("<q", b8)[0]
            out["as_double"] = _struct.unpack("<d", b8)[0]
            out["as_pointer"] = hex(_struct.unpack("<Q", b8)[0])
    except (ValueError, struct.error):  # pragma: no cover - 防御
        pass
    return out
