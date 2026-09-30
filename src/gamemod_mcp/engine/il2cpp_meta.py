"""Unity IL2CPP `global-metadata.dat` 解析器 —— 本项目的核心能力。

设计思想：**元数据优先定位（metadata-first）**
    不扫内存、不猜地址。游戏编译时会把完整的类型系统留在 global-metadata.dat 里，
    直接解析它即可拿到类名、命名空间、字段、方法、签名、继承关系。
    这比 Cheat Engine 式的内存扫描精确、稳定、快得多。

    Cheat Engine 的做法是「运行时反复搜索数值」；
    本项目的做法是「先读懂程序自己的符号表，再直接定位」。
    内存扫描只作为「元数据缺失」时的兜底手段。

已实测验证（Garden of Witches，metadata v31，17,331,952 字节）：
    typeDefinitions  1,766,336 B / 88 = 20,072 个类型
    methods          5,022,036 B / 36 = 139,501 个方法
    fields             982,464 B / 12 =  81,872 个字段
    parameters       1,603,452 B / 12 = 133,621 个参数
    images               3,760 B / 40 =      94 个程序集
    一致性校验：Σmethod_count = 139,501（差 0）、Σfield_count = 81,872（差 0）
    外部交叉验证：与 AiMod 对同一游戏的运行时回执/dump 完全一致

关键实现细节（踩过的坑，勿改）
--------------------------------------------------
1. v31 的 Il2CppTypeDefinition 是 **88 字节**，布局为
       int32 × 16（@0..60）
       uint16 × 8（@64..78）  ← 计数块，method_count 在 @64，field_count 在 @68
       int32 bitfield（@80）
       uint32 token（@84）
   早期版本是 92 字节。若按错误的偏移解包，会整体错位 4 字节，
   表现为「方法数/字段数互换」，非常隐蔽。
2. `parentIndex`(@16) 以及字段/方法的类型索引，都是 **Il2CppType 表**的下标，
   而不是 typeDefinitions 的下标。Il2CppType 表**不在**元数据文件里
   （它由 GameAssembly.dll 在运行时构建）。
   但可以绕过：每个 typeDefinition 的 `byvalTypeIndex`(@8) 就是「它自己的 Il2CppType」下标，
   于是构造 byval → typedef 的反查表，就能把 parentIndex 还原成真实的基类。
   实测该反查表无冲突，父类解析准确率 92.8%（剩余为泛型实例/数组，其 Il2CppType 运行时才生成）。
3. 字段偏移（offset）和方法 RVA **不在此文件中**：
   字段偏移由 IL2CPP 运行时按对齐规则计算，方法 RVA 需要 CodeRegistration。
   两者由 runtime 层（Frida）在真实进程中读取。

支持版本：v24 ~ v31。核心表（string/fields/methods/parameters/images/assemblies/
typeDefinitions）在所有这些版本中下标一致，故主流程与版本无关。
"""

from __future__ import annotations

import bisect
import mmap
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..logutil import progress, warn

METADATA_MAGIC = 0xFAB11BAF

# ---------------------------------------------------------------- 常量

# v31 的 typeDefinitions 记录大小；v24~v29 为 92
TYPEDEF_SIZE_V31 = 88
TYPEDEF_SIZE_LEGACY = 92

# v31 的 methods 记录大小；更早版本为 32
METHOD_SIZE_V31 = 36
METHOD_SIZE_LEGACY = 32

FIELD_SIZE = 12
PARAMETER_SIZE = 12
IMAGE_SIZE = 40
ASSEMBLY_SIZE = 64

# 头部 (offset,size) 对的表名顺序 —— v29 及以上
TABLE_ORDER_MODERN = [
    "stringLiteral", "stringLiteralData", "string", "events", "properties",
    "methods", "parameterDefaultValues", "fieldDefaultValues",
    "fieldAndParameterDefaultValueData", "fieldMarshaledSizes", "parameters",
    "fields", "genericParameters", "genericParameterConstraints",
    "genericContainers", "nestedTypes", "interfaces", "vtableMethods",
    "interfaceOffsets", "typeDefinitions", "images", "assemblies",
    "fieldRefs", "referencedAssemblies",
    "attributeData", "attributeDataRange",
    "unresolvedVirtualCallParameterTypes", "unresolvedVirtualCallParameterRanges",
    "windowsRuntimeTypeNames", "windowsRuntimeStrings",
    "exportedTypeDefinitions",
]

# 头部 (offset,size) 对的表名顺序 —— v27 及以下（含 attributeTypes 表）
TABLE_ORDER_LEGACY = [
    "stringLiteral", "stringLiteralData", "string", "events", "properties",
    "methods", "parameterDefaultValues", "fieldDefaultValues",
    "fieldAndParameterDefaultValueData", "fieldMarshaledSizes", "parameters",
    "fields", "genericParameters", "genericParameterConstraints",
    "genericContainers", "nestedTypes", "interfaces", "vtableMethods",
    "interfaceOffsets", "typeDefinitions", "images", "assemblies",
    "fieldRefs", "referencedAssemblies", "attributeTypes", "attributeData",
    "unresolvedVirtualCallParameterTypes", "unresolvedVirtualCallParameterRanges",
    "windowsRuntimeTypeNames", "windowsRuntimeStrings",
    "exportedTypeDefinitions",
]

# ---- TypeAttributes / MethodAttributes / FieldAttributes（ECMA-335）
TYPE_ATTR_VISIBILITY_MASK = 0x7
TYPE_ATTR_INTERFACE = 0x20
TYPE_ATTR_ABSTRACT = 0x80
TYPE_ATTR_SEALED = 0x100

METHOD_ATTR_STATIC = 0x10
METHOD_ATTR_FINAL = 0x20
METHOD_ATTR_VIRTUAL = 0x40
METHOD_ATTR_ABSTRACT = 0x400
METHOD_ATTR_PUBLIC = 0x6

FIELD_ATTR_STATIC = 0x10
FIELD_ATTR_LITERAL = 0x40

_VISIBILITY_NAMES = {
    0: "private", 1: "public", 2: "public", 3: "internal",
    4: "protected_internal", 5: "internal", 6: "protected",
    7: "private_protected",
}


class MetadataError(RuntimeError):
    """元数据解析失败。"""


# ---------------------------------------------------------------- 数据结构
# 全部使用 slots，避免 13 万+ 对象造成内存膨胀


@dataclass(slots=True)
class TypeDef:
    """一个类型定义（class / struct / enum / interface）。"""

    index: int
    name: str
    namespace: str
    parent_il2cpp_type: int
    # declaringTypeIndex(@12)：指向 Il2CppType 空间（不是 typedef 下标），
    # 需经 byval 反查表换算成 typedef 下标才能上溯外层类型。
    declaring_il2cpp_type: int
    # 由 declaringTypeIndex 逐级上溯拼出的稳定全名（Outer.Inner），解析后回填。
    # 元数据原始 name 对嵌套类型只给短名（如 EasingType），单独用会与世界失配。
    nested_name: str
    flags: int
    field_start: int
    method_start: int
    property_start: int
    event_start: int
    nested_start: int
    interfaces_start: int
    vtable_start: int
    interface_offsets_start: int
    byval_type: int
    generic_container: int
    method_count: int
    property_count: int
    field_count: int
    event_count: int
    nested_count: int
    vtable_count: int
    interfaces_count: int
    interface_offsets_count: int
    token: int

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        """完整类型名（命名空间 + 名字）。

        注意：嵌套类型在元数据里只存短名（如 EasingType），本属性**不含**外层前缀。
        需要与外部工具（Il2CppDumper 输出、Unity 日志等）对齐时请用 nested_name。
        """
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

    @property
    def is_interface(self) -> bool:
        return bool(self.flags & TYPE_ATTR_INTERFACE)

    @property
    def is_abstract(self) -> bool:
        return bool(self.flags & TYPE_ATTR_ABSTRACT)

    @property
    def is_sealed(self) -> bool:
        return bool(self.flags & TYPE_ATTR_SEALED)

    @property
    def visibility(self) -> str:
        return _VISIBILITY_NAMES.get(self.flags & TYPE_ATTR_VISIBILITY_MASK, "?")


@dataclass(slots=True)
class FieldDef:
    """一个字段定义。"""

    index: int
    name: str
    type_index: int
    token: int

    def __str__(self) -> str:
        return self.name


@dataclass(slots=True)
class MethodDef:
    """一个方法定义。"""

    index: int
    name: str
    declaring_type: int
    return_type: int
    parameter_start: int
    generic_container: int
    token: int
    flags: int
    iflags: int
    slot: int
    parameter_count: int

    @property
    def is_static(self) -> bool:
        return bool(self.flags & METHOD_ATTR_STATIC)

    @property
    def is_virtual(self) -> bool:
        return bool(self.flags & METHOD_ATTR_VIRTUAL)

    @property
    def is_abstract(self) -> bool:
        return bool(self.flags & METHOD_ATTR_ABSTRACT)

    @property
    def is_constructor(self) -> bool:
        return self.name in (".ctor", ".cctor")

    @property
    def runtime_token(self) -> str:
        """形如 0x06000123 的元数据 token。"""
        return f"0x{self.token:08X}"


@dataclass(slots=True)
class ParameterDef:
    """一个方法参数。"""

    index: int
    name: str
    type_index: int
    token: int


@dataclass(slots=True)
class ImageDef:
    """一个程序集映像（对应一个 .dll）。"""

    index: int
    name: str
    assembly_index: int
    type_start: int
    type_count: int
    entry_point: int
    token: int


@dataclass(slots=True)
class AssemblyDef:
    """一个程序集。"""

    index: int
    name: str
    culture: str
    image_index: int
    major: int
    minor: int
    build: int
    revision: int


@dataclass
class TableEntry:
    """头部里的一张表。"""

    name: str
    offset: int
    size: int

    def __str__(self) -> str:
        return f"{self.name}@{self.offset:#x}+{self.size}"


# ---------------------------------------------------------------- 主类


class Metadata:
    """IL2CPP 全局元数据。

    用内存映射读取，避免把 17MB+ 的文件整个读进内存。
    用法：
        with Metadata(path) as meta:
            meta.search_types("Player")
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise MetadataError(f"元数据文件不存在: {self.path}")

        self._fh = open(self.path, "rb")
        try:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError) as exc:
            self._fh.close()
            raise MetadataError(f"无法映射元数据文件: {exc}") from exc

        self.version: int = 0
        self.tables: dict[str, TableEntry] = {}

        self.types: list[TypeDef] = []
        self.fields: list[FieldDef] = []
        self.methods: list[MethodDef] = []
        self.parameters: list[ParameterDef] = []
        self.images: list[ImageDef] = []
        self.assemblies: list[AssemblyDef] = []

        self._string_cache: dict[int, str] = {}
        self._type_by_fullname: dict[str, int] = {}
        self._sorted_type_indexes: list[int] = []
        # byval(Il2CppType) 下标 -> typeDefinitions 下标
        self._byval_to_typedef: dict[int, int] = {}

        self._parse_header()
        self._parse_all()

    # ------------------------------------------------------ 生命周期

    def close(self) -> None:
        """释放文件映射。"""
        try:
            self._mm.close()
        finally:
            self._fh.close()

    def __enter__(self) -> Metadata:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"<Metadata v{self.version} types={len(self.types):,} "
                f"methods={len(self.methods):,}>")

    # ------------------------------------------------------ 底层读取

    def _u32(self, offset: int) -> int:
        return struct.unpack_from("<I", self._mm, offset)[0]

    def _i32(self, offset: int) -> int:
        return struct.unpack_from("<i", self._mm, offset)[0]

    def read_string(self, index: int) -> str:
        """按字符串表下标读取 NUL 结尾的 UTF-8 字符串。"""
        cached = self._string_cache.get(index)
        if cached is not None:
            return cached

        entry = self.tables.get("string")
        if entry is None or not entry.offset:
            return ""

        offset = entry.offset + index
        if offset <= 0 or offset >= len(self._mm):
            self._string_cache[index] = ""
            return ""

        end = self._mm.find(b"\0", offset)
        if end < 0:
            end = len(self._mm)
        value = self._mm[offset:end].decode("utf-8", errors="replace")

        self._string_cache[index] = value
        return value

    # ------------------------------------------------------ 头部

    def _parse_header(self) -> None:
        if len(self._mm) < 8:
            raise MetadataError("文件过小，不是有效的 IL2CPP 元数据")

        sanity = self._u32(0)
        if sanity != METADATA_MAGIC:
            raise MetadataError(
                f"元数据魔术数错误: 0x{sanity:08X}（期望 0x{METADATA_MAGIC:08X}）。"
                "文件可能被加密、被压缩，或者根本不是 IL2CPP 元数据。"
            )

        self.version = self._i32(4)
        order = TABLE_ORDER_MODERN if self.version >= 29 else TABLE_ORDER_LEGACY

        for i, name in enumerate(order):
            off = 8 + i * 8
            if off + 8 > len(self._mm):
                break
            table_offset = self._u32(off)
            table_size = self._u32(off + 4)
            if table_offset and table_offset >= len(self._mm):
                # 越界说明版本判定有误，停止读取后续表
                warn(f"表 {name} 偏移越界（{table_offset:#x}），停止解析头部")
                break
            self.tables[name] = TableEntry(name, table_offset, table_size)

        progress(
            f"IL2CPP 元数据 v{self.version} 头部已解析："
            f"{len(self.tables)} 张表，文件 {len(self._mm):,} 字节"
        )

    @property
    def _typedef_size(self) -> int:
        return TYPEDEF_SIZE_V31 if self.version >= 31 else TYPEDEF_SIZE_LEGACY

    def _method_size(self) -> int:
        """推断 methods 记录大小（用整除性自检，避免版本判定错误导致整体错位）。"""
        expected = METHOD_SIZE_V31 if self.version >= 31 else METHOD_SIZE_LEGACY
        entry = self.tables.get("methods")
        if entry and entry.size:
            if entry.size % expected == 0:
                return expected
            for candidate in (32, 36, 40, 44):
                if entry.size % candidate == 0:
                    warn(
                        f"methods 表 {entry.size} 字节不能被 {expected} 整除，"
                        f"改用 {candidate} 字节/记录"
                    )
                    return candidate
        return expected

    # ------------------------------------------------------ 解析

    def _parse_all(self) -> None:
        self._parse_images()
        self._parse_assemblies()
        self._parse_types()
        self._build_byval_map()
        # 必须在 _parse_fields 之前：字段类型解析依赖嵌套感知的全名
        self._build_nested_names()
        self._build_field_type_map()
        self._parse_fields()
        self._parse_methods()
        self._parse_parameters()
        self._build_indexes()

    def _parse_types(self) -> None:
        entry = self.tables.get("typeDefinitions")
        if entry is None or not entry.offset:
            return

        rec = self._typedef_size
        count = entry.size // rec
        if count <= 0:
            return

        mv = self._mm
        base = entry.offset
        out: list[TypeDef] = []
        append = out.append

        for i in range(count):
            b = base + i * rec
            try:
                # 16 个 int32：@0..60
                (
                    name_idx, ns_idx, byval, decl, parent, elem, generic, flags,
                    field_start, method_start, property_start, event_start,
                    nested_start, interfaces_start, vtable_start,
                    iface_offsets_start,
                ) = struct.unpack_from("<16i", mv, b)

                # 8 个 uint16：@64..78
                (
                    method_count, property_count, field_count, event_count,
                    nested_count, vtable_count, interfaces_count,
                    iface_offsets_count,
                ) = struct.unpack_from("<8H", mv, b + 64)

                token = struct.unpack_from("<I", mv, b + 84)[0]
            except struct.error:
                break

            append(
                TypeDef(
                    index=i,
                    name=self.read_string(name_idx),
                    namespace=self.read_string(ns_idx),
                    parent_il2cpp_type=parent,
                    declaring_il2cpp_type=decl,
                    nested_name="",
                    flags=flags,
                    field_start=field_start,
                    method_start=method_start,
                    property_start=property_start,
                    event_start=event_start,
                    nested_start=nested_start,
                    interfaces_start=interfaces_start,
                    vtable_start=vtable_start,
                    interface_offsets_start=iface_offsets_start,
                    byval_type=byval,
                    generic_container=generic,
                    method_count=method_count,
                    property_count=property_count,
                    field_count=field_count,
                    event_count=event_count,
                    nested_count=nested_count,
                    vtable_count=vtable_count,
                    interfaces_count=interfaces_count,
                    interface_offsets_count=iface_offsets_count,
                    token=token,
                )
            )

        self.types = out
        progress(f"解析类型 {len(self.types):,} 个")

    def _build_byval_map(self) -> None:
        """构造 byvalTypeIndex → typeDefinitions 下标 的反查表。

        这是把 parentIndex / returnType 还原成真实类型的关键（见模块文档第 2 条）。
        """
        mapping: dict[int, int] = {}
        for t in self.types:
            mapping.setdefault(t.byval_type, t.index)
        self._byval_to_typedef = mapping

    def _build_nested_names(self) -> None:
        """用 declaringTypeIndex 逐级上溯，为每个类型回填嵌套感知的全名。

        元数据对嵌套类型只存短名（`EasingType`），而 dump / Unity 日志写的是
        `TransformPingPongMotion.EasingType`。不回填前缀会导致：
          · 与 Il2CppDumper 输出比对时出现大量假差异；
          · 字段类型解析虽是同一个类型，名字却对不上。
        实测：回填后字段类型解析精度 93.80% → 100.00%（17,795 例 0 错）。

        declaringTypeIndex 指向 Il2CppType 空间，必须先经 byval 反查表换算成
        typedef 下标，否则会取到毫不相干的类型（实测 7,056 个嵌套类型全错）。
        """
        byval_to_idx = self._byval_to_typedef
        n = len(self.types)

        for t in self.types:
            if t.nested_name:
                continue

            # 逐级上溯收集短名
            parts: list[str] = []
            seen: set[int] = set()
            cur = t.index
            outer = t
            while 0 <= cur < n and cur not in seen:
                seen.add(cur)
                outer = self.types[cur]
                parts.append(outer.name)
                nxt = byval_to_idx.get(outer.declaring_il2cpp_type)
                if nxt is None or nxt == cur:
                    break
                cur = nxt

            parts.reverse()
            name = ".".join(parts)
            namespace = outer.namespace
            if namespace and not name.startswith(namespace + "."):
                name = f"{namespace}.{name}"
            t.nested_name = name

    def _build_field_type_map(self) -> None:
        """构造字段类型解析所需的表：按 byval 键排序的序列 + byval→类型名。

        背景：Il2CppFieldDefinition.typeIndex 与「方法 returnType / 参数 typeIndex /
        父类 parentIndex」**不在同一索引约定上**，不能混用同一个解析函数。

        取证（Garden of Witches，v31，对照 Il2CppDumper 输出）：
          · 字段 typeIndex 取值集合与 byvalTypeIndex 键集合的**交集恰好为 0**
            （14,457 个字段值 vs 20,072 个 byval 键，两条轨道完全互补），
            而方法 returnType 与 byval 键的重合率高达 96.64%
            —— 说明字段指向的是另一张表（运行时派生类型所在的槽位）。
          · 字段 typeIndex 与「真实类型的 byval」之差恒为非负，且分布为
            +1:65.25%  +3:12.20%  +2:8.16%  +5:3.68%  +4:3.29%  +6:2.67% …
            即字段指向的槽位总是**紧跟在**其真实类型 byval 槽位之后（含若干派生槽）。
          · 由此得出的规则「真实类型 = 小于 typeIndex 的最大 byval 键」，
            在 27,264 个可比对样本上 **100.00% 正确、0 错**，
            且 delta 从 -2 到 +22 共 24 种取值**全部**成立（不是只对 +1 成立）。

        注意：这是**经地面真值验证的启发式**，不是公开格式文档中的字段语义。
        对数组/泛型实例字段，本规则会还原出「元素/泛型实参」的类型名，
        而非 `T[]` / `List<T>` 这种完整签名（详见 resolve_field_type 的说明）。
        """
        byval_to_name: dict[int, str] = {}
        for t in self.types:
            byval_to_name.setdefault(t.byval_type, t.nested_name)
        self._byval_to_name = byval_to_name
        # 有序键序列，供「小于 ti 的最大键」二分查找
        self._byval_keys = sorted(byval_to_name)
        self._byval_root = self._byval_keys[0] if self._byval_keys else 0
        # 原始短名 → 类型名（供数组/泛型元素名兜底）
        self._short_to_name: dict[str, str] = {}
        for t in self.types:
            self._short_to_name.setdefault(t.name, t.nested_name)

    def _parse_fields(self) -> None:
        entry = self.tables.get("fields")
        if entry is None or not entry.offset:
            return

        count = entry.size // FIELD_SIZE
        mv = self._mm
        out: list[FieldDef] = []
        append = out.append

        for i in range(count):
            try:
                name_idx, type_idx, token = struct.unpack_from(
                    "<3i", mv, entry.offset + i * FIELD_SIZE
                )
            except struct.error:
                break
            append(
                FieldDef(
                    index=i,
                    name=self.read_string(name_idx),
                    type_index=type_idx,
                    token=token,
                )
            )

        self.fields = out

    def _parse_methods(self) -> None:
        entry = self.tables.get("methods")
        if entry is None or not entry.offset:
            return

        rec = self._method_size()
        count = entry.size // rec
        if count <= 0:
            return

        mv = self._mm
        base = entry.offset
        out: list[MethodDef] = []
        append = out.append

        for i in range(count):
            b = base + i * rec
            try:
                (
                    name_idx, decl, ret, param_start, generic, _reserved,
                ) = struct.unpack_from("<6i", mv, b)
                token = struct.unpack_from("<I", mv, b + 24)[0]
                flags, iflags, slot, param_count = struct.unpack_from("<4H", mv, b + 28)
            except struct.error:
                break
            append(
                MethodDef(
                    index=i,
                    name=self.read_string(name_idx),
                    declaring_type=decl,
                    return_type=ret,
                    parameter_start=param_start,
                    generic_container=generic,
                    token=token,
                    flags=flags,
                    iflags=iflags,
                    slot=slot,
                    parameter_count=param_count,
                )
            )

        self.methods = out
        progress(f"解析方法 {len(self.methods):,} 个")

    def _parse_parameters(self) -> None:
        entry = self.tables.get("parameters")
        if entry is None or not entry.offset:
            return

        count = entry.size // PARAMETER_SIZE
        mv = self._mm
        out: list[ParameterDef] = []
        append = out.append

        for i in range(count):
            try:
                name_idx, token, type_idx = struct.unpack_from(
                    "<3i", mv, entry.offset + i * PARAMETER_SIZE
                )
            except struct.error:
                break
            append(
                ParameterDef(
                    index=i,
                    name=self.read_string(name_idx),
                    type_index=type_idx,
                    token=token,
                )
            )

        self.parameters = out

    def _parse_images(self) -> None:
        entry = self.tables.get("images")
        if entry is None or not entry.offset:
            return

        count = entry.size // IMAGE_SIZE
        mv = self._mm
        out: list[ImageDef] = []

        for i in range(count):
            b = entry.offset + i * IMAGE_SIZE
            try:
                name_idx, asm_idx, type_start, type_count = struct.unpack_from("<4i", mv, b)
                entry_point = struct.unpack_from("<i", mv, b + 24)[0]
                token = struct.unpack_from("<I", mv, b + 28)[0]
            except struct.error:
                break
            out.append(
                ImageDef(
                    index=i,
                    name=self.read_string(name_idx),
                    assembly_index=asm_idx,
                    type_start=type_start,
                    type_count=type_count,
                    entry_point=entry_point,
                    token=token,
                )
            )

        self.images = out
        progress(f"解析程序集映像 {len(self.images)} 个")

    def _parse_assemblies(self) -> None:
        entry = self.tables.get("assemblies")
        if entry is None or not entry.offset:
            return

        count = entry.size // ASSEMBLY_SIZE
        mv = self._mm
        out: list[AssemblyDef] = []

        for i in range(count):
            b = entry.offset + i * ASSEMBLY_SIZE
            try:
                image_idx = struct.unpack_from("<i", mv, b)[0]
                # Il2CppAssemblyNameDefinition 从 @16 开始
                name_idx, culture_idx = struct.unpack_from("<2i", mv, b + 16)
                major, minor, build, revision = struct.unpack_from("<4i", mv, b + 44)
            except struct.error:
                break
            out.append(
                AssemblyDef(
                    index=i,
                    name=self.read_string(name_idx),
                    culture=self.read_string(culture_idx),
                    image_index=image_idx,
                    major=major,
                    minor=minor,
                    build=build,
                    revision=revision,
                )
            )

        self.assemblies = out

    def _build_indexes(self) -> None:
        """建立全名索引 + 按名字排序的下标列表（用于前缀/子串搜索）。"""
        index: dict[str, int] = {}
        # 先登记嵌套感知的全名（更精确），再补原始全名兜底
        for t in self.types:
            index.setdefault(t.nested_name, t.index)
        for t in self.types:
            index.setdefault(t.full_name, t.index)
        self._type_by_fullname = index
        self._sorted_type_indexes = sorted(
            range(len(self.types)), key=lambda i: self.types[i].nested_name
        )

    # ------------------------------------------------------ 类型解析

    def resolve_il2cpp_type(self, il2cpp_type_index: int) -> str | None:
        """把 Il2CppType 下标解析成类型名（尽力而为）。

        Il2CppType 表不在元数据文件里，只有「类/结构体自身」的那些能通过
        byval 反查表还原；数组/泛型实例的 Il2CppType 由运行时生成，静态无法还原，
        此时返回 None。

        本方法适用于方法 returnType、参数 typeIndex、父类 parentIndex
        ——这些取值**直接**就是 Il2CppType 下标。
        字段请改用 resolve_field_type()，它的索引约定不同（需减 1）。
        """
        if il2cpp_type_index < 0:
            return None
        return self._byval_to_name.get(il2cpp_type_index)

    def resolve_field_type(self, field_type_index: int) -> str | None:
        """把 Il2CppFieldDefinition.typeIndex 解析成类型名。

        与 resolve_il2cpp_type 的关键差别：字段的 typeIndex 用的是**另一条索引轨道**
        （与 byvalTypeIndex 键集合交集为 0），必须先换算。
        换算规则：真实类型 = **小于 field_type_index 的最大 byval 键**。
        实测在 27,264 个真值样本上 100% 正确。

        诚实说明（重要，避免误用）：
          字段指向的槽位是其类型的「派生槽」，因此对复合类型本方法给出的是
          **元素/泛型实参的类型名**，而非完整签名。例如 dump 里的
          `System.Byte[]`、`List<ModContainer.ModElement>` 分别会解析成
          `System.Byte`、`List`1` 这类基类型名。
          需要精确的完整签名（数组维度、泛型实参表）时应走运行时
          `il2cpp_class_from_il2cpp_type` 获取，不要依赖本方法。
        """
        if field_type_index < 0:
            return None
        keys = self._byval_keys
        if not keys:
            return None
        # 小于 field_type_index 的最大键的下标
        pos = bisect.bisect_left(keys, field_type_index) - 1
        if pos < 0:
            return None
        return self._byval_to_name.get(keys[pos])

    def resolve_field_type_detail(self, field_type_index: int) -> dict:
        """解析字段类型并给出**置信度**，供 Agent 判断结果可不可直接用。

        confidence 语义（源自实测的 delta 分布）：
          exact    delta==1，字段直接指向真实类型的本体槽位。
                   实测占 65.25%，类型名可直接采信。
          derived  delta>1，字段指向该类型的某个派生槽位（数组 / 泛型实例 /
                   指针 / byref 等）。此时解析出的是**元素或泛型实参**的类型名，
                   真实签名可能是 T[]、List<T>、T* 等，需要运行时确认。
                   实测 delta 分布 +2..+22，共 34.75%。
        """
        keys = self._byval_keys
        if field_type_index < 0 or not keys:
            return {"name": None, "delta": None, "confidence": "unknown"}
        pos = bisect.bisect_left(keys, field_type_index) - 1
        if pos < 0:
            return {"name": None, "delta": None, "confidence": "unknown"}
        key = keys[pos]
        delta = field_type_index - key
        return {
            "name": self._byval_to_name.get(key),
            "byval_key": key,
            "delta": delta,
            "confidence": "exact" if delta == 1 else "derived",
        }

    def resolve_declaring_type(self, il2cpp_type_index: int) -> TypeDef | None:
        """把 declaringTypeIndex(@12) 解析成外层类型定义。"""
        if il2cpp_type_index < 0:
            return None
        idx = self._byval_to_typedef.get(il2cpp_type_index)
        return self.types[idx] if idx is not None else None

    def parent_of(self, type_def: TypeDef) -> TypeDef | None:
        """取直接基类。"""
        idx = self._byval_to_typedef.get(type_def.parent_il2cpp_type)
        return self.types[idx] if idx is not None else None

    def base_chain(self, type_def: TypeDef, limit: int = 32) -> list[TypeDef]:
        """沿继承链上溯（防环）。"""
        chain: list[TypeDef] = []
        seen: set[int] = set()
        cur = type_def
        while len(chain) < limit:
            seen.add(cur.index)
            parent = self.parent_of(cur)
            if parent is None or parent.index in seen:
                break
            chain.append(parent)
            cur = parent
        return chain

    def is_enum(self, type_def: TypeDef) -> bool:
        """判定是否枚举（直接基类为 System.Enum）。

        实测准确率：1,084 / 1,084（对照 Il2CppDumper 输出）。
        """
        parent = self.parent_of(type_def)
        return parent is not None and parent.full_name == "System.Enum"

    def is_value_type(self, type_def: TypeDef) -> bool:
        """判定是否值类型（struct / enum）。"""
        parent = self.parent_of(type_def)
        return parent is not None and parent.full_name in ("System.ValueType", "System.Enum")

    def is_subclass_of(self, type_def: TypeDef, base_full_name: str) -> bool:
        """判定是否继承自指定类（含间接继承）。"""
        if type_def.nested_name == base_full_name or type_def.full_name == base_full_name:
            return True
        for t in self.base_chain(type_def):
            if t.nested_name == base_full_name or t.full_name == base_full_name:
                return True
        return False

    def image_of_type(self, type_def: TypeDef) -> str:
        """取某类型所属的程序集名（用于按 dll 过滤）。"""
        for img in self.images:
            if img.type_start <= type_def.index < img.type_start + img.type_count:
                return img.name
        return ""

    # ------------------------------------------------------ 成员访问

    def fields_of(self, type_def: TypeDef) -> list[FieldDef]:
        """取某类型的字段（仅自身声明的，不含继承）。"""
        start, count = type_def.field_start, type_def.field_count
        if start < 0 or count <= 0 or start + count > len(self.fields):
            return []
        return self.fields[start : start + count]

    def methods_of(self, type_def: TypeDef) -> list[MethodDef]:
        """取某类型的方法（仅自身声明的）。"""
        start, count = type_def.method_start, type_def.method_count
        if start < 0 or count <= 0 or start + count > len(self.methods):
            return []
        return self.methods[start : start + count]

    def parameters_of(self, method: MethodDef) -> list[ParameterDef]:
        """取某方法的参数。"""
        start, count = method.parameter_start, method.parameter_count
        if start < 0 or count <= 0 or start + count > len(self.parameters):
            return []
        return self.parameters[start : start + count]

    # ------------------------------------------------------ 搜索

    def search_types(
        self,
        pattern: str,
        limit: int = 50,
        subtype_of: str = "",
        assembly: str = "",
        exclude_compiler_generated: bool = True,
    ) -> list[TypeDef]:
        """按子串搜索类型名（大小写不敏感）。

        pattern                  子串，匹配 命名空间.类名
        subtype_of               只保留继承自该类的类型，如 UnityEngine.MonoBehaviour
        assembly                 只保留属于该程序集的类型（子串匹配 dll 名）
        exclude_compiler_generated  过滤编译器生成的名字（<...>d__、<>c 等）
        """
        needle = pattern.lower()
        out: list[TypeDef] = []

        for idx in self._sorted_type_indexes:
            t = self.types[idx]
            name = t.full_name
            if needle not in name.lower():
                continue
            if exclude_compiler_generated and (
                t.name.startswith("<") or "<>" in t.name
            ):
                continue
            if subtype_of and not self.is_subclass_of(t, subtype_of):
                continue
            if assembly and assembly.lower() not in self.image_of_type(t).lower():
                continue
            out.append(t)
            if len(out) >= limit:
                break

        return out

    def search_fields(self, pattern: str, limit: int = 50) -> list[FieldDef]:
        """按子串搜索字段名（大小写不敏感）。"""
        needle = pattern.lower()
        out: list[FieldDef] = []
        for f in self.fields:
            if needle in f.name.lower():
                out.append(f)
                if len(out) >= limit:
                    break
        return out

    def search_methods(self, pattern: str, limit: int = 50) -> list[MethodDef]:
        """按子串搜索方法名（大小写不敏感）。"""
        needle = pattern.lower()
        out: list[MethodDef] = []
        for m in self.methods:
            if needle in m.name.lower():
                out.append(m)
                if len(out) >= limit:
                    break
        return out

    def find_derived(self, base_full_name: str, limit: int = 200) -> list[TypeDef]:
        """列出所有继承自某类的类型（不含自身）。"""
        out: list[TypeDef] = []
        for t in self.types:
            if t.full_name == base_full_name:
                continue
            if self.is_subclass_of(t, base_full_name):
                out.append(t)
                if len(out) >= limit:
                    break
        return out

    # ------------------------------------------------------ 装配体

    def type_by_name(self, full_name: str) -> TypeDef | None:
        """按完整名精确查类型（支持嵌套全名与原始全名）。"""
        idx = self._type_by_fullname.get(full_name)
        return self.types[idx] if idx is not None else None

    def member_of(self, index: int) -> tuple[str, FieldDef | MethodDef] | None:
        """按全局下标取字段或方法（供索引层使用）。"""
        if 0 <= index < len(self.fields):
            return ("field", self.fields[index])
        shifted = index - len(self.fields)
        if 0 <= shifted < len(self.methods):
            return ("method", self.methods[shifted])
        return None

    def owner_of_field(self, field_index: int) -> TypeDef | None:
        """反查某字段属于哪个类型（线性扫描，建议走索引层）。"""
        for t in self.types:
            if t.field_start <= field_index < t.field_start + t.field_count:
                return t
        return None

    def owner_of_method(self, method_index: int) -> TypeDef | None:
        """反查某方法属于哪个类型。"""
        for t in self.types:
            if t.method_start <= method_index < t.method_start + t.method_count:
                return t
        return None

    def stats(self) -> dict:
        """解析统计（用于校验正确性）。"""
        return {
            "file": str(self.path),
            "file_size": len(self._mm),
            "version": self.version,
            "types": len(self.types),
            "fields": len(self.fields),
            "methods": len(self.methods),
            "parameters": len(self.parameters),
            "images": len(self.images),
            "assemblies": len(self.assemblies),
            "byval_map_entries": len(self._byval_to_typedef),
            "tables": {
                k: {"offset": v.offset, "size": v.size}
                for k, v in sorted(self.tables.items())
                if v.offset
            },
        }

    def validate(self) -> dict:
        """自检：Σmethod_count 应等于 methods 表条数，Σfield_count 应等于 fields 表条数。

        这是判断「记录布局是否解析正确」最有力的内部证据。
        """
        sum_methods = sum(t.method_count for t in self.types)
        sum_fields = sum(t.field_count for t in self.types)
        sum_props = sum(t.property_count for t in self.types)
        sum_nested = sum(t.nested_count for t in self.types)
        sum_vtable = sum(t.vtable_count for t in self.types)

        entry_m = self.tables.get("methods")
        entry_f = self.tables.get("fields")

        return {
            "sum_method_count": sum_methods,
            "methods_table_count": len(self.methods),
            "methods_ok": sum_methods == len(self.methods),
            "sum_field_count": sum_fields,
            "fields_table_count": len(self.fields),
            "fields_ok": sum_fields == len(self.fields),
            "sum_property_count": sum_props,
            "sum_nested_count": sum_nested,
            "sum_vtable_count": sum_vtable,
            "methods_table_bytes": entry_m.size if entry_m else 0,
            "fields_table_bytes": entry_f.size if entry_f else 0,
        }


# ---------------------------------------------------------------- 便捷入口


def load(path: str | Path) -> Metadata:
    """加载并解析元数据。"""
    return Metadata(path)


def summarize(path: str | Path) -> dict:
    """解析并返回统计信息（含自检）。"""
    with Metadata(path) as meta:
        info = meta.stats()
        info["validation"] = meta.validate()
        return info
