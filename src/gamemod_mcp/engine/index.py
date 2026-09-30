"""索引层：把解析出的元数据落成可高速查询的 SQLite 库（带缓存）。

为什么要单独做索引层
--------------------------------------------------
`global-metadata.dat` 有 17 MB、2 万个类、13 万个方法。
每次 Agent 提问都重新解析一遍是不可接受的（约 2 秒），
而 Agent 在一次任务里会问几十上百次。

因此这里做两件事：
    1. **持久化缓存**：以元数据文件的 sha256 为键，建一次 SQLite，之后秒开。
    2. **进程内缓存**：同一个 session 内复用已打开的连接。

关于 `read_query_result` / `search_query_result`
--------------------------------------------------
AiMod 的这两个工具本质上解决同一个问题：**工具返回值不能太大**。
Agent 的上下文有限，一次返回 13 万个方法会把上下文炸掉。
本项目的做法与之等价但更简单：
    所有列表类查询都返回 `{items, total, offset, limit, has_more}`，
    并且把完整结果写进一个 **查询结果句柄表**，
    之后可用 `read_query_result(handle, offset, limit)` 翻页，
    或用 `search_query_result(handle, pattern)` 在结果内部再过滤。
这样既保证单次返回精简，又保证信息不丢失。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..logutil import progress, warn
from . import adapt
from .il2cpp_meta import METADATA_MAGIC, Metadata, MetadataError
from .mono_meta import Metadata as MonoMetadata

# ---------------------------------------------------------------- 缓存目录

# 2：types 表增加 nested_name（嵌套感知全名）、declaring_il2cpp 列；
#    字段类型改由 Metadata.resolve_field_type 解析（索引约定与其它成员不同）
SCHEMA_VERSION = 2


def default_cache_dir() -> Path:
    """默认缓存目录：用户主目录下的 .gamemod/cache。"""
    import os

    base = os.environ.get("GAMEMOD_CACHE_DIR")
    if base:
        return Path(base)
    return Path.home() / ".gamemod" / "cache"


def _file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    """流式计算文件摘要（17 MB 文件约 30 ms）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- 查询结果句柄


@dataclass
class QueryResult:
    """一次查询的结果句柄。

    大结果集不会一次性返回，而是留在这里供后续翻页/过滤。
    """

    handle: str
    kind: str
    total: int
    items: list[dict[str, Any]]

    def page(self, offset: int, limit: int) -> dict[str, Any]:
        """按偏移取一页。"""
        offset = max(0, offset)
        limit = max(1, min(limit, 2000))
        chunk = self.items[offset : offset + limit]
        return {
            "handle": self.handle,
            "kind": self.kind,
            "offset": offset,
            "limit": limit,
            "returned": len(chunk),
            "total": self.total,
            "has_more": offset + len(chunk) < self.total,
            "items": chunk,
        }

    def search(self, pattern: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """在已有结果内部按子串过滤（大小写不敏感）。"""
        needle = pattern.lower()
        hits = [
            it for it in self.items
            if needle in " ".join(str(v) for v in it.values()).lower()
        ]
        offset = max(0, offset)
        limit = max(1, min(limit, 2000))
        chunk = hits[offset : offset + limit]
        return {
            "handle": self.handle,
            "pattern": pattern,
            "offset": offset,
            "limit": limit,
            "returned": len(chunk),
            "total": len(hits),
            "has_more": offset + len(chunk) < len(hits),
            "items": chunk,
        }


class ResultStore:
    """查询结果句柄表（进程内，有上限，超出后淘汰最旧的）。"""

    def __init__(self, max_entries: int = 32):
        self._max = max_entries
        self._items: dict[str, QueryResult] = {}
        self._counter = 0

    def put(self, kind: str, items: list[dict[str, Any]]) -> QueryResult:
        """登记一份结果，返回句柄。"""
        self._counter += 1
        handle = f"{kind}-{self._counter}"
        qr = QueryResult(handle=handle, kind=kind, total=len(items), items=items)
        self._items[handle] = qr

        # 淘汰最旧的
        while len(self._items) > self._max:
            oldest = next(iter(self._items))
            del self._items[oldest]

        return qr

    def get(self, handle: str) -> QueryResult | None:
        return self._items.get(handle)

    def handles(self) -> list[str]:
        return list(self._items)


# ---------------------------------------------------------------- 索引主体


class Index:
    """IL2CPP 元数据索引。

    典型用法：
        with Index.open(metadata_path) as idx:
            idx.search_classes("Player")
    """

    def __init__(self, meta: Any, db_path: Path):
        self.meta = meta
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.results = ResultStore()
        # 这个索引是「复用既有缓存」还是「刚刚全量重建」——
        # 只有 open() 知道，所以由它赋值，默认按「新建」算。
        # 之前 index_info 里硬编码 True，导致重建一次（几秒到几十秒）
        # 也会报 cache_hit=true，Agent 据此以为走了快路径，无法察觉异常。
        self.cache_hit = False
        self._build()

    # ------------------------------------------------------ 生命周期

    @staticmethod
    def _load_meta(metadata_path: Path, engine: str = "") -> Any:
        """按引擎载入元数据解析器。

        engine 为空或 auto 时**自动嗅探**：先看 magic（IL2CPP 的
        global-metadata.dat 以 0xFAB11BAF 开头），不是 IL2CPP 就试 Mono。
        显式传入 engine 可以跳过嗅探、避免误判。
        """
        want = (engine or "auto").strip().lower()
        if want in ("auto", "", "unknown"):
            want = Index._sniff_engine(metadata_path)

        if want == "il2cpp":
            return Metadata(metadata_path)
        if want == "mono":
            return MonoMetadata(metadata_path)

        # 显式指定了不认识的引擎：直接报错，不要静默回退到某个解析器。
        # 静默回退正是「Agent 拿到貌似成功但内容错乱的结果」的根源。
        raise MetadataError(
            f"不支持的引擎类型：{engine!r}（可用：il2cpp / mono / auto）"
        )

    @staticmethod
    def _sniff_engine(metadata_path: Path) -> str:
        """读文件头判断引擎。"""
        try:
            with open(metadata_path, "rb") as fh:
                head = fh.read(8)
        except OSError:
            return "il2cpp"
        # IL2CPP global-metadata.dat 的 magic 是 0xFAB11BAF（小端）
        if len(head) >= 4 and int.from_bytes(head[:4], "little") == METADATA_MAGIC:
            return "il2cpp"
        # .NET 程序集以 "MZ" 开头（PE 文件）
        if head[:2] == b"MZ":
            return "mono"
        # 兜底：按原行为当 IL2CPP 处理（会给出明确报错而非静默错数据）
        return "il2cpp"

    @classmethod
    def open(
        cls,
        metadata_path: str | Path,
        cache_dir: str | Path | None = None,
        force_rebuild: bool = False,
        engine: str = "",
    ) -> "Index":
        """打开（必要时构建）索引。

        缓存键 = 元数据文件 sha256，因此换游戏/换版本会自动重建，不会串数据。
        engine 为空时自动嗅探（IL2CPP / Mono）。
        """
        metadata_path = Path(metadata_path)
        meta = cls._load_meta(metadata_path, engine)
        eng = adapt.engine_of(meta)

        cache = Path(cache_dir) if cache_dir else default_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)

        digest = _file_sha256(metadata_path)
        # 文件名带引擎前缀：同一个文件被两种解析器读出的索引不共用
        db_path = cache / f"{eng}-{digest[:16]}.sqlite"

        if db_path.is_file() and not force_rebuild and cls._is_valid(db_path):
            progress(f"复用索引缓存 {db_path.name}")
            idx = cls.__new__(cls)
            idx.meta = meta
            idx.db_path = db_path
            idx.conn = sqlite3.connect(str(db_path))
            idx.conn.row_factory = sqlite3.Row
            idx.results = ResultStore()
            idx.cache_hit = True          # ← 真的复用了缓存
            return idx

        if db_path.is_file() and force_rebuild:
            db_path.unlink()

        progress(f"构建索引 {db_path.name}（{adapt.engine_label(meta)}）...")
        idx = cls(meta, db_path)          # __init__ 里 cache_hit=False
        return idx

    @staticmethod
    def _is_valid(db_path: Path) -> bool:
        """校验缓存文件完整（schema 版本匹配且表非空）。"""
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                if row is None or int(row[0]) != SCHEMA_VERSION:
                    return False
                n = conn.execute("SELECT COUNT(*) FROM types").fetchone()[0]
                return n > 0
            finally:
                conn.close()
        except sqlite3.Error:
            return False

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self.meta.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Index {self.db_path.name}>"

    # ------------------------------------------------------ 建表

    def _build(self) -> None:
        cur = self.conn.cursor()

        cur.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS types (
                idx            INTEGER PRIMARY KEY,
                name           TEXT NOT NULL,
                namespace      TEXT NOT NULL,
                full_name      TEXT NOT NULL,
                nested_name    TEXT NOT NULL,
                declaring_il2cpp INTEGER,
                parent_il2cpp  INTEGER,
                flags          INTEGER,
                token          INTEGER,
                method_count   INTEGER,
                field_count    INTEGER,
                is_enum        INTEGER,
                is_value_type  INTEGER,
                image          TEXT
            );

            CREATE TABLE IF NOT EXISTS fields (
                idx        INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                type_index INTEGER,
                owner      INTEGER
            );

            CREATE TABLE IF NOT EXISTS methods (
                idx             INTEGER PRIMARY KEY,
                name            TEXT NOT NULL,
                owner           INTEGER,
                return_type     INTEGER,
                parameter_start INTEGER,
                parameter_count INTEGER,
                flags           INTEGER,
                token           INTEGER
            );

            CREATE TABLE IF NOT EXISTS parameters (
                idx        INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                type_index INTEGER
            );

            CREATE TABLE IF NOT EXISTS images (
                idx        INTEGER PRIMARY KEY,
                name       TEXT,
                type_start INTEGER,
                type_count INTEGER
            );
            """
        )

        m = self.meta
        eng = adapt.engine_of(m)

        # ---- meta
        cur.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("metadata_file", str(m.path)),
                ("metadata_version", str(m.version)),
                ("engine", eng),
                ("engine_label", adapt.engine_label(m)),
                ("type_count", str(len(m.types))),
                ("method_count", str(len(m.methods))),
                ("field_count", str(len(m.fields))),
            ],
        )

        # ---- types
        # 注意 declaring/parent 两个引用列的语义随引擎不同（见 adapt 模块），
        # 它们只用于「有没有外层/有没有基类」的判断，不参与进一步索引。
        enum_flags = []
        for t in m.types:
            enum_flags.append(
                (
                    t.index, t.name, t.namespace, t.full_name,
                    getattr(t, "nested_name", "") or t.full_name,
                    adapt.declaring_ref(m, t) or 0,
                    adapt.parent_ref(m, t) or 0,
                    t.flags, t.token, t.method_count, t.field_count,
                    1 if m.is_enum(t) else 0,
                    1 if m.is_value_type(t) else 0,
                    adapt.image_of_type(m, t),
                )
            )
        cur.executemany(
            "INSERT INTO types VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", enum_flags
        )

        # ---- fields（附带 owner，省掉运行时的线性反查）
        field_rows = []
        for t in m.types:
            if t.field_count <= 0:
                continue
            for f in m.fields_of(t):
                field_rows.append(
                    (f.index, f.name, adapt.field_type_key(m, f), t.index)
                )
        cur.executemany("INSERT INTO fields VALUES (?,?,?,?)", field_rows)

        # ---- methods（owner 取 declaring_type 的静态归属）
        method_rows = []
        for t in m.types:
            if t.method_count <= 0:
                continue
            for mm in m.methods_of(t):
                method_rows.append(
                    (mm.index, mm.name, t.index, adapt.method_return_key(m, mm),
                     mm.parameter_start, mm.parameter_count, mm.flags, mm.token)
                )
        cur.executemany("INSERT INTO methods VALUES (?,?,?,?,?,?,?,?)", method_rows)

        # ---- parameters
        # Mono 的 Param 表不含类型（类型只在方法签名里），故 type_index 为 None
        param_rows = [
            (p.index, p.name, adapt.param_type_key(m, p)) for p in m.parameters
        ]
        cur.executemany("INSERT INTO parameters VALUES (?,?,?)", param_rows)

        # ---- images（Mono 的 ImageDef 没有 type_start/type_count，做兜底）
        image_rows = []
        for img in m.images:
            image_rows.append(
                (
                    img.index,
                    getattr(img, "name", "") or "",
                    getattr(img, "type_start", None),
                    getattr(img, "type_count", None),
                )
            )
        cur.executemany("INSERT INTO images VALUES (?,?,?,?)", image_rows)

        # ---- 索引
        cur.executescript(
            """
            CREATE INDEX IF NOT EXISTS ix_types_fullname ON types(full_name);
            CREATE INDEX IF NOT EXISTS ix_types_nested   ON types(nested_name);
            CREATE INDEX IF NOT EXISTS ix_types_name     ON types(name);
            CREATE INDEX IF NOT EXISTS ix_types_enum     ON types(is_enum);
            CREATE INDEX IF NOT EXISTS ix_types_image    ON types(image);
            CREATE INDEX IF NOT EXISTS ix_fields_name    ON fields(name);
            CREATE INDEX IF NOT EXISTS ix_fields_owner   ON fields(owner);
            CREATE INDEX IF NOT EXISTS ix_methods_name   ON methods(name);
            CREATE INDEX IF NOT EXISTS ix_methods_owner  ON methods(owner);
            """
        )

        self.conn.commit()
        progress(
            f"索引构建完成：类型 {len(enum_flags):,}、字段 {len(field_rows):,}、"
            f"方法 {len(method_rows):,}、参数 {len(param_rows):,}"
        )

    # ------------------------------------------------------ 查询：类型

    def index_info(self) -> dict[str, Any]:
        """索引概况。

        `cache_hit` 如实反映「这次是复用缓存还是真重建」：
        false 意味着刚刚花了时间做全量解析，是排查「为什么慢」的第一手线索。
        """
        cur = self.conn.cursor()
        info = dict(cur.execute("SELECT key, value FROM meta").fetchall())
        # meta 表里存的是文本，这里把计数字段转成 int，
        # 免得 Agent 拿到 "20072" 这种字符串还要自己转。
        for key in ("schema_version", "metadata_version",
                    "type_count", "method_count", "field_count"):
            if key in info:
                try:
                    info[key] = int(info[key])
                except (TypeError, ValueError):
                    pass
        info["database"] = str(self.db_path)
        info["database_size"] = self.db_path.stat().st_size if self.db_path.is_file() else 0
        info["enum_count"] = cur.execute(
            "SELECT COUNT(*) FROM types WHERE is_enum=1"
        ).fetchone()[0]
        info["image_count"] = cur.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        info["cache_hit"] = bool(getattr(self, "cache_hit", False))
        # 把「这个引擎在元数据层面能提供什么」一并透给 Agent，
        # 免得它把「IL2CPP 本来就没有字段 flags」误判成工具出错。
        info["capabilities"] = adapt.capabilities(self.meta)
        return info

    # ------------------------------------------------------ 跨引擎解析收口
    #
    # 下面三个方法把「同一个语义在不同引擎下的不同解析方式」收在一处。
    # 上层查询代码只调它们，不再直接碰 meta.resolve_il2cpp_type 之类的
    # 引擎专有接口。
    #
    # 背景：IL2CPP 的 returnType / paramTypeIndex / parentIndex 都是
    # Il2CppType 索引，用同一个 resolve 就行；Mono 的这三者分别是
    # 签名 blob、签名内顺序、TypeDefOrRef 编码值，需要分开处理。

    def _resolve_param(self, method_idx: int, param_row: sqlite3.Row
                       ) -> str | None:
        """解析参数类型。

        IL2CPP 可以逐参数用 typeIndex 解；
        Mono 的 Param 表没有类型，必须从**所属方法**的签名里按顺序取。
        """
        if adapt.engine_of(self.meta) == adapt.ENGINE_IL2CPP:
            return adapt.resolve_method_return(self.meta,
                                               param_row["type_index"])
        # Mono：用方法签名里的参数类型列表按 sequence 取
        try:
            types = adapt.resolve_param_types(self.meta,
                                              self.meta.methods[method_idx])
            idx = param_row["sequence"] - 1   # sequence 是 1 基
            if 0 <= idx < len(types):
                return types[idx]
        except Exception:
            pass
        return None

    def _resolve_parent(self, type_idx: int) -> str | None:
        """解析类型的基类名。"""
        try:
            name = adapt.parent_name(self.meta, self.meta.types[type_idx])
        except Exception:
            return None
        return name or None

    def _row_to_type(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "index": row["idx"],
            "name": row["name"],
            "namespace": row["namespace"],
            "full_name": row["full_name"],
            # 嵌套感知全名（Outer.Inner）；嵌套类型用它与外部工具对齐
            "nested_name": row["nested_name"],
            "parent": self._resolve_parent(row["idx"]),
            "image": row["image"],
            "is_enum": bool(row["is_enum"]),
            "is_value_type": bool(row["is_value_type"]),
            "field_count": row["field_count"],
            "method_count": row["method_count"],
            "token": f"0x{row['token']:08X}",
        }

    def search_classes(
        self,
        pattern: str = "",
        offset: int = 0,
        limit: int = 50,
        subtype_of: str = "",
        assembly: str = "",
        only_enums: bool = False,
        exclude_compiler_generated: bool = True,
    ) -> dict[str, Any]:
        """搜索类/结构/枚举 —— Mod 开发的入口查询。

        pattern    子串（大小写不敏感），可空表示不过滤
        subtype_of 只保留继承自该类的类型（如 UnityEngine.MonoBehaviour）
        assembly   按 dll 名过滤（子串）
        only_enums 只要枚举（找游戏里的常量表特别有用）
        """
        where: list[str] = []
        args: list[Any] = []

        if pattern:
            # 同时匹配嵌套全名与原始全名，避免嵌套类型搜不到
            where.append("(nested_name LIKE ? OR full_name LIKE ?)")
            args.extend([f"%{pattern}%", f"%{pattern}%"])
        if assembly:
            where.append("image LIKE ?")
            args.append(f"%{assembly}%")
        if only_enums:
            where.append("is_enum = 1")
        if exclude_compiler_generated:
            where.append("name NOT LIKE '<%' AND name NOT LIKE '%<>%'")

        sql = "SELECT * FROM types"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY nested_name"

        rows = list(self.conn.execute(sql, args))

        # subtype_of 需要走继承链，无法纯 SQL 表达，故在 Python 侧过滤
        if subtype_of:
            rows = [
                r for r in rows
                if self.meta.is_subclass_of(self.meta.types[r["idx"]], subtype_of)
            ]

        total = len(rows)
        limit = max(1, min(limit, 2000))
        offset = max(0, offset)
        page = rows[offset : offset + limit]
        items = [self._row_to_type(r) for r in page]

        qr = self.results.put("classes", [self._row_to_type(r) for r in rows])

        return {
            "handle": qr.handle,
            "total": total,
            "offset": offset,
            "limit": limit,
            "returned": len(items),
            "has_more": offset + len(items) < total,
            "items": items,
        }

    def get_type(self, full_name: str) -> dict[str, Any] | None:
        """按完整名精确取类型（支持嵌套全名与原始全名）。

        与 `class_detail` 走同一套唯一性解析：拿不准就返回 `None`，
        **不**在多个同名类里随便挑一个（原来的 `LIMIT 1` 正是这么错的）。

        注：当前仓库内没有调用方（实际入口是 `class_detail`），
        保留是因为它是「按名取类型」的公开小工具，将来可能被复用；
        若确认长期无人使用，可连同这段注释一起删除。
        """
        ids, err = self._match_type_ids(full_name)
        if err or not ids:
            return None
        row = self.conn.execute(
            "SELECT * FROM types WHERE idx = ? LIMIT 1", (ids[0],)).fetchone()
        return self._row_to_type(row) if row else None

    # ------------------------------------------------------ 查询：成员

    def _member_row(self, row: sqlite3.Row, kind: str, type_name: str) -> dict[str, Any]:
        keys = row.keys()
        return {
            "index": row["idx"],
            "name": row["name"],
            "type": type_name,
            "return_type": (
                adapt.resolve_method_return(self.meta, row["return_type"])
                if kind == "method" and "return_type" in keys
                else None
            ),
            "type_index": row["type_index"] if "type_index" in keys else None,
            "owner": row["owner"],
        }

    # 句柄里最多留多少条。超过这个数就不再往句柄里堆，但 `total` 仍然是**真实总数**
    # （见下面 _paged 的说明）——「句柄不全」必须如实标注，不能悄悄截断。
    _HANDLE_CAP = 20000

    def _match_type_ids(
        self, name: str, assembly: str = "", label: str = "owner"
    ) -> tuple[list[int], dict[str, Any] | None]:
        """把类名解析成**唯一**一个类型，必要时按 assembly 消歧。

        返回 `(类型 idx 列表, 错误)`；成功时列表恰好一个元素。

        为什么必须唯一（历史教训）：早期实现是
        `SELECT idx FROM types WHERE nested_name=? OR full_name=? OR name=?`，
        然后把**所有**命中的类型当同一个类来处理：

            输入 "UserData" → 命中 2 个类型
                UserData                              [Unity.InputSystem.dll]
                TeamTapas.ProjectR.UserDatas.UserData [TeamTapas.ProjectR.dll]

        工具照样返回 `ok: true`，把 Unity 的字段和游戏自己的字段**混在一起**。
        Agent 以为拿到的是「UserData 这个类的字段」，据此算偏移必然错，
        而且错误要到运行时才暴露。所以命中多个时**拒绝并给出候选**，
        让调用方补 `assembly` 消歧，而不是替它猜。

        为什么必须把「全名」与「短名」两种匹配**合并**后再判歧义：
        没有命名空间的类型，其 `full_name` **就等于**短名
        （实测 `UserData` 的 `full_name` 就是 `"UserData"`）。
        若先单独试全名匹配，会「恰好命中一个」而看不出歧义，
        于是静默选中了 Unity 那个 —— 比原来的 bug 更隐蔽。
        合并后：全名匹配 {Unity} ∪ 短名匹配 {Unity, 团队那个} = 2 个 → 正确判为歧义。

        碰撞规模是实测过的（不是拍脑袋加限制）：
        IL2CPP 样本 20,072 个类型里，会被判为歧义的名字只有 585 个（3%）；
        Mono 样本 1,387 个类型里是 0 个。也就是说「拒绝」几乎不会误伤，
        而它拦掉的正是那种「看起来成功、偏移全错」的静默错误。
        """
        def _ids(where: str, *args: Any) -> list[int]:
            return [r["idx"] for r in self.conn.execute(
                f"SELECT DISTINCT idx FROM types WHERE {where}", args)]

        # 全名/嵌套全名匹配 + 短名匹配，取并集（保持稳定顺序）
        cand = _ids("nested_name = ? OR full_name = ?", name, name)
        for i in _ids("name = ?", name):
            if i not in cand:
                cand.append(i)

        # assembly 过滤必须在判歧义**之前**做：它是调用方给的消歧依据。
        # 注意不能因为 len==1 就跳过 —— 那样「指定了错误的 assembly」
        # 会静默返回那个不属于该程序集的类（实测：assembly="TeamTapas"
        # 却返回了 Unity 的 UserData）。
        if assembly and cand:
            ph = ",".join("?" * len(cand))
            keep = {r["idx"] for r in self.conn.execute(
                f"SELECT idx FROM types WHERE idx IN ({ph}) AND image LIKE ?",
                (*cand, f"%{assembly}%"))}
            cand = [i for i in cand if i in keep]

        if not cand:
            if assembly:
                return [], {"error": f"未找到类：{name}（在程序集 "
                                     f"{assembly!r} 里不存在，请检查拼写或去掉 assembly）",
                            "total": 0, "items": []}
            return [], {"error": f"未找到类：{name}"
                                 f"（用 search_classes 搜短名以确认完整类名）",
                        "total": 0, "items": []}

        if len(cand) > 1:
            return [], self._type_ambiguous(name, cand, assembly, label)
        return cand, None

    def _resolve_owner(
        self, owner: str, assembly: str = ""
    ) -> tuple[list[int], dict[str, Any] | None]:
        """`search_fields` / `search_methods` 的 owner 解析（见 `_match_type_ids`）。"""
        return self._match_type_ids(owner, assembly, label="owner")

    def _resolve_type_row(
        self, name: str, assembly: str = ""
    ) -> tuple[sqlite3.Row | None, dict[str, Any] | None]:
        """按名字解析出**唯一**一行的 `types` 记录（供 class_detail / class_hierarchy 用）。

        与 `search_classes` 不同，这几个工具返回的是「这个类的全部内容」，
        拿错类不是少几条数据，而是整份字段/方法表都是别人的 ——
        所以这里同样必须拒绝歧义，而不是 `LIMIT 1` 随便选一个。

        历史教训：`class_detail`/`class_hierarchy` 曾用
        `... LIMIT 1` + 退化到 `name = ? LIMIT 1`，
        实测 `class_detail("UserData")` 静默返回了 Unity.InputSystem.dll
        那个 UserData（11 个字段），而调用方多半想要游戏自己的那个
        （23 个字段），且返回里没有任何歧义提示。这与
        `search_fields` 的 owner 合并是同一类缺陷，只是表现成「选错」而非「混合」。
        """
        ids, err = self._match_type_ids(name, assembly, label="full_name")
        if err:
            # 索引层统一用 found=False 表达「没查到」，server 的 _index_query
            # 会把它转成 ok=False 并把 candidates/hint 一并透出。
            out: dict[str, Any] = {"found": False,
                                   "reason": err.get("error", f"未找到类：{name}")}
            for k, v in err.items():
                if k not in ("error", "total", "items"):
                    out[k] = v
            return None, out
        row = self.conn.execute(
            "SELECT * FROM types WHERE idx = ? LIMIT 1", (ids[0],)).fetchone()
        if row is None:  # 理论上不可能：idx 刚从同一张表取出来
            return None, {"found": False, "reason": f"未找到类：{name}"}
        return row, None

    def _type_ambiguous(
        self, name: str, ids: list[int], assembly: str, label: str = "owner"
    ) -> dict[str, Any]:
        """类名命中多个类型：拒绝，并把候选摆出来（参照 hook_method 的做法）。"""
        cands = []
        for r in self.conn.execute(
            f"SELECT idx, nested_name, full_name, name, image, field_count, "
            f"method_count FROM types WHERE idx IN ({','.join('?' * len(ids))})",
            tuple(ids),
        ):
            cands.append({
                "index": r["idx"],
                "full_name": r["full_name"],
                "nested_name": r["nested_name"],
                "name": r["name"],
                "image": r["image"],
                "field_count": r["field_count"],
                "method_count": r["method_count"],
            })
        cands.sort(key=lambda c: (c["image"] or "", c["nested_name"] or ""))
        why = ("（已按 assembly 过滤，但仍不唯一）" if assembly else
               "（多半是不同程序集里有同名类）")
        best = cands[0]
        return {
            "error": f"{label}={name!r} 命中 {len(cands)} 个类{why}，"
                     f"无法确定你要哪一个。请改用完整类名，或用 assembly 参数消歧。",
            "total": 0,
            "items": [],
            "candidates": cands,
            "hint": f'{label}="{best["full_name"]}"'
                    + (f' 或 {label}="{name}", assembly="{assembly}"' if assembly
                       else f' 或 {label}="{name}", assembly="{best["image"]}"'),
        }

    def _paged(
        self, kind: str, total: int, items: list[dict[str, Any]],
        offset: int, limit: int, handle_truncated: bool, **extra: Any
    ) -> dict[str, Any]:
        """组装统一的分页返回（`total` 是真值，`has_more` 如实标注）。

        关键：`total` 由调用方用 `COUNT(*)` 算出来，**不是** `len(items)`。
        早期实现在 SQL 里写死 `LIMIT 20000` 再令 `total = len(items)`，
        于是「'a' 命中 49286 个字段」被报成 20000（实测），
        而且是**静默**的：Agent 会以为全局只有 2 万个匹配，据此判断覆盖率。
        """
        limit = max(1, min(limit, 2000))
        offset = max(0, offset)
        page = items[offset : offset + limit]
        out = {
            "handle": self.results.put(kind, items).handle,
            "total": total,
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "has_more": offset + len(page) < total,
            "items": page,
        }
        if handle_truncated:
            out["handle_truncated"] = True
            out["handle_note"] = (
                f"匹配数 {total} 超过句柄上限 {self._HANDLE_CAP}，"
                f"句柄只保留了前 {len(items)} 条；翻页请用本工具的 offset/limit，"
                f"不要依赖 read_query_result 拿到全集。"
            )
        out.update(extra)
        return out

    def search_fields(
        self, pattern: str, offset: int = 0, limit: int = 50, owner: str = "",
        assembly: str = "",
    ) -> dict[str, Any]:
        """按字段名搜索（大小写不敏感）。

        owner 非空时只保留属于**某一个**类的字段，该类由 owner 唯一确定：
        先按完整名/嵌套全名匹配，再退到短名；命中多个则拒绝并给候选
        （可用 assembly 消歧）。很多字段名（如 value、data、count）在几万个
        类里重复，不给 owner 会淹没在噪声里。
        """
        where = "name LIKE ?"
        params: list[Any] = [f"%{pattern}%"]
        if owner:
            owners, bad = self._resolve_owner(owner, assembly)
            if bad:
                return bad
            where += f" AND owner IN ({','.join('?' * len(owners))})"
            params.extend(owners)

        # total 走 COUNT(*) 拿真值；items 才受句柄上限约束
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM fields WHERE {where}", params).fetchone()[0]
        rows = list(self.conn.execute(
            f"SELECT * FROM fields WHERE {where} ORDER BY name LIMIT ?",
            (*params, self._HANDLE_CAP)))

        items = []
        for r in rows:
            dt = adapt.resolve_field(self.meta, r["type_index"])
            items.append(
                {
                    "index": r["idx"],
                    "name": r["name"],
                    "owner": (
                        self.meta.types[r["owner"]].nested_name
                        if r["owner"] is not None
                        else None
                    ),
                    # 字段的类型引用与其它成员不是同一套索引，必须走字段专用解析
                    "field_type": dt.get("name"),
                    "type_confidence": dt.get("confidence"),
                    "type_index": r["type_index"],
                }
            )
        return self._paged("fields", total, items, offset, limit,
                           total > len(items))

    def search_methods(
        self, pattern: str, offset: int = 0, limit: int = 50, owner: str = "",
        assembly: str = "",
    ) -> dict[str, Any]:
        """按方法名搜索（大小写不敏感）。

        owner 非空时只保留属于**某一个**类的方法，语义与 search_fields
        完全一致（含「命中多个就拒绝」）。如搜 Update 时限定 Player。
        """
        where = "name LIKE ?"
        params: list[Any] = [f"%{pattern}%"]
        if owner:
            owners, bad = self._resolve_owner(owner, assembly)
            if bad:
                return bad
            where += f" AND owner IN ({','.join('?' * len(owners))})"
            params.extend(owners)

        total = self.conn.execute(
            f"SELECT COUNT(*) FROM methods WHERE {where}", params).fetchone()[0]
        rows = list(self.conn.execute(
            f"SELECT * FROM methods WHERE {where} ORDER BY name LIMIT ?",
            (*params, self._HANDLE_CAP)))

        items = [
            {
                "index": r["idx"],
                "name": r["name"],
                "owner": self.meta.types[r["owner"]].full_name if r["owner"] is not None else None,
                "return_type": adapt.resolve_method_return(self.meta,
                                                           r["return_type"]),
                "parameter_count": r["parameter_count"],
                "flags": r["flags"],
                "token": f"0x{r['token']:08X}",
            }
            for r in rows
        ]
        return self._paged("methods", total, items, offset, limit,
                           total > len(items))

    def class_detail(self, full_name: str, include_inherited: bool = False,
                     assembly: str = "") -> dict[str, Any]:
        """取一个类的完整信息：字段（含静态判定）、方法、基类链、接口、子类数。

        这是「搞清楚一个类长什么样」的主力工具，也是后续 Hook 的输入。
        `assembly` 用于同名类消歧（见 `_match_type_ids`）。
        """
        # 必须唯一解析：返回的是「这个类的全部内容」，选错类等于整份字段表
        # 都是别人的，且返回里看不出异常（实测 UserData 会静默落到 Unity 那个）。
        row, err = self._resolve_type_row(full_name, assembly)
        if err:
            return err
        assert row is not None

        t = self.meta.types[row["idx"]]
        m = self.meta

        fields = [
            {
                "index": f.index,
                "name": f.name,
                "type_index": adapt.field_type_key(m, f),
                # 字段走专用解析规则；type_confidence 区分「直接命中」与
                # 「派生槽位（数组/泛型实例，只给到元素类型名）」
                "type": dt.get("name"),
                "type_confidence": dt.get("confidence"),
                # delta 只有 IL2CPP 有（索引轨道偏移量）；Mono 是精确签名，无此概念
                "type_delta": dt.get("delta"),
                "declared_by": t.nested_name,
            }
            for f in m.fields_of(t)
            for dt in (adapt.resolve_field(m, adapt.field_type_key(m, f)),)
        ]

        methods = [
            {
                "index": x.index,
                "name": x.name,
                "token": x.runtime_token,
                "return_type": adapt.resolve_method_return(
                    m, adapt.method_return_key(m, x)),
                "parameter_count": x.parameter_count,
                "parameters": [
                    {"name": p.name, "type": pt}
                    for p, pt in zip(
                        m.parameters_of(x),
                        adapt.resolve_param_types(m, x) or
                        [None] * len(m.parameters_of(x)),
                    )
                ],
                "is_static": x.is_static,
                "is_virtual": x.is_virtual,
                "is_constructor": x.is_constructor,
                "flags": f"0x{x.flags:04X}",
            }
            for x in m.methods_of(t)
        ]

        chain = [c.nested_name for c in m.base_chain(t)]

        if include_inherited:
            for anc in m.base_chain(t):
                for f in m.fields_of(anc):
                    dt = adapt.resolve_field(m, adapt.field_type_key(m, f))
                    fields.append(
                        {
                            "index": f.index,
                            "name": f.name,
                            "type_index": adapt.field_type_key(m, f),
                            "type": dt.get("name"),
                            "type_confidence": dt.get("confidence"),
                            "declared_by": anc.nested_name,
                        }
                    )
                for x in m.methods_of(anc):
                    methods.append(
                        {
                            "index": x.index,
                            "name": x.name,
                            "token": x.runtime_token,
                            "return_type": adapt.resolve_method_return(
                                m, adapt.method_return_key(m, x)),
                            # 继承来的方法同样要给参数表：class_detail 的
                            # method_name 过滤（原 get_method_source 的职责）
                            # 会同时命中自身与继承的方法，字段形状必须一致，
                            # 否则 Agent 按 parameter_count 选重载时会拿到 None。
                            "parameter_count": x.parameter_count,
                            "parameters": [
                                {"name": p.name, "type": pt}
                                for p, pt in zip(
                                    m.parameters_of(x),
                                    adapt.resolve_param_types(m, x) or
                                    [None] * len(m.parameters_of(x)),
                                )
                            ],
                            "flags": f"0x{x.flags:04X}",
                            "is_static": x.is_static,
                            "is_virtual": x.is_virtual,
                            "is_constructor": x.is_constructor,
                            "declared_by": anc.nested_name,
                        }
                    )

        # 全量取回，这样 derived_count 才是真值（不是被 200 截断后的数）。
        # 曾经写死 limit=200，于是 UnityEngine.MonoBehaviour 报 derived_count=200，
        # 实际有 1548 个 —— 与 find_derived_classes 的 total 是同一类谎报。
        derived = m.find_derived(t.full_name, limit=1_000_000)
        derived_cap = 50

        return {
            "found": True,
            "index": t.index,
            "full_name": t.full_name,
            "nested_name": t.nested_name,
            "namespace": t.namespace,
            "name": t.name,
            "image": m.image_of_type(t),
            "token": f"0x{t.token:08X}",
            "flags": f"0x{t.flags:08X}",
            "visibility": t.visibility,
            "is_enum": m.is_enum(t),
            "is_value_type": m.is_value_type(t),
            "is_interface": t.is_interface,
            "is_abstract": t.is_abstract,
            "is_sealed": t.is_sealed,
            "parent": self._resolve_parent(t.index),
            "base_chain": chain,
            "field_count": len(fields),
            "method_count": len(methods),
            "fields": fields,
            "methods": methods,
            "derived_count": len(derived),
            "derived": [d.nested_name for d in derived[:derived_cap]],
            # 如实标注列表被截断，避免 Agent 把 50 条当成全部
            "derived_returned": min(len(derived), derived_cap),
            "derived_has_more": len(derived) > derived_cap,
            "include_inherited": include_inherited,
        }

    # ------------------------------------------------------ 查询：层级 / 依赖

    def class_hierarchy(self, full_name: str, direction: str = "both", depth: int = 6,
                       assembly: str = "") -> dict[str, Any]:
        """类的继承层级树。

        direction: up（基类）/ down（子类）/ both
        """
        m = self.meta
        # 与 class_detail 同一套解析：继承树挂到别的同名类上同样是无提示的错误。
        _row, err = self._resolve_type_row(full_name, assembly)
        if err:
            return err
        t = m.types[_row["idx"]]

        result: dict[str, Any] = {"found": True, "root": t.nested_name}

        if direction in ("up", "both"):
            result["up"] = [c.nested_name for c in m.base_chain(t, limit=depth)]

        if direction in ("down", "both"):
            tree: list[dict[str, Any]] = []
            frontier = [(t, 0)]
            seen = {t.index}
            while frontier:
                cur, lvl = frontier.pop(0)
                if lvl >= depth:
                    continue
                for d in m.find_derived(cur.nested_name, limit=500):
                    if d.index in seen:
                        continue
                    seen.add(d.index)
                    tree.append({"name": d.nested_name, "depth": lvl + 1,
                                 "parent": cur.nested_name})
                    frontier.append((d, lvl + 1))
            result["down"] = tree
            result["down_count"] = len(tree)

        return result

    def find_derived_classes(self, base_full_name: str, limit: int = 200,
                             assembly: str = "") -> dict[str, Any]:
        """列出所有继承自某类的类型（找 MonoBehaviour 脚本的必备工具）。

        判定用**传递式**继承（含间接继承），因此
        `find_derived_classes("UnityEngine.MonoBehaviour")` 能一次拿到
        所有脚本组件 —— 这正是它取代旧 `find_monobehaviours` 的原因：
        那个工具只是本方法加了一个「只看游戏程序集」的粗糙过滤器，
        而 `assembly` 参数能把这件事做得更明确、更可控。

        Args:
            base_full_name: 基类完整名。
            limit:          最多返回多少条。
            assembly:       只看该程序集（子串匹配，如 "Assembly-CSharp"）。
                            留空则不限。找游戏自己的 MonoBehaviour 脚本时
                            传 "Assembly-CSharp" 比按前缀猜引擎程序集更可靠。

        注意：基类**不必**出现在被索引的程序集里。Mono 游戏只索引
        Assembly-CSharp.dll，而 `UnityEngine.MonoBehaviour` 定义在
        UnityEngine.CoreModule.dll —— 这时 `type_by_name` 找不到基类，
        但 `find_derived` 依然能正确工作（它扫的是派生类的基类引用）。
        所以这里**不能**因为找不到基类就报错。

        返回的 `total` **必须**是过滤后的**真实总数**，不是被 limit 截断后的条数：
        实测 Archon Soul 的 MonoBehaviour 派生类有 644 个，若按截断数返回
        `total: 200`，Agent 会以为「这个游戏只有 200 个脚本」而漏掉三分之二。
        所以这里全量算出结果集，再用 `has_more` 如实标注是否被截断。
        全量扫描很便宜（实测 IL2CPP 20,072 类型 17 ms / Mono 1,387 类型 2 ms），
        不值得为此牺牲正确性。
        """
        m = self.meta
        base = m.type_by_name(base_full_name)

        # 全量取回，再按 assembly 过滤 —— 这样 total 才是真值。
        # 之前先用 limit*20 截断再过滤，导致 total 既不是真值、也不稳定。
        derived = m.find_derived(base_full_name, limit=1_000_000)

        filtered = []
        for d in derived:
            img = m.image_of_type(d)
            if assembly and assembly.lower() not in img.lower():
                continue
            filtered.append({
                "index": d.index,
                "full_name": d.nested_name,
                "image": img,
                "field_count": d.field_count,
                "method_count": d.method_count,
            })

        total = len(filtered)
        items = filtered[:limit]
        truncated = total > limit

        if base is None and total == 0:
            # 既找不到基类、也没有任何派生类 —— 这才是真的查不到
            return {"found": False, "query": base_full_name,
                    "total": 0, "returned": 0, "items": []}

        out: dict[str, Any] = {
            "found": True,
            "base": base.nested_name if base is not None else base_full_name,
            "total": total,
            "returned": len(items),
            "has_more": truncated,
            "items": items,
        }
        if truncated:
            out["note"] = (
                f"匹配 {total} 个，本次只返回前 {len(items)} 个（limit={limit}）。"
                f"调大 limit，或加 assembly= 缩小范围。"
            )
        if assembly:
            out["assembly_filter"] = assembly
        if base is None:
            # 如实说明：基类不在当前索引范围内，但派生类是通过基类名字符串
            # 比对出来的，结果依然有效。
            # 注意：不能直接赋值覆盖 —— 可能上面已有「被截断」的提示，
            # 两条信息都要保留（否则截断警告会被悄悄吞掉）。
            extra = (
                f"基类 {base_full_name} 不在当前索引的程序集内（例如它属于 "
                f"UnityEngine/System 等框架程序集），派生类按基类名比对得出。"
            )
            out["note"] = f"{out['note']} {extra}" if out.get("note") else extra
        return out

    def analyze_dependency(self, full_name: str,
                           assembly: str = "") -> dict[str, Any]:
        """静态依赖概览：这个类引用了哪些类型、被谁引用。

        注意：**这是基于元数据签名的近似分析**，不是精确的调用图。
        IL2CPP 不保留托管 IL，精确调用图需要反汇编原生代码。
        本工具给出的是「字段/方法签名里出现了哪些类型」，用于快速摸清耦合关系。
        """
        m = self.meta
        # 同 class_detail：依赖清单必须属于**请求的那个类**，不能选错同名类。
        _row, err = self._resolve_type_row(full_name, assembly)
        if err:
            return err
        t = m.types[_row["idx"]]

        referenced: dict[str, int] = {}

        def bump(nm: str | None) -> None:
            if nm and nm != t.nested_name:
                referenced[nm] = referenced.get(nm, 0) + 1

        for f in m.fields_of(t):
            # 字段走专用解析规则
            bump(adapt.resolve_field(m, adapt.field_type_key(m, f)).get("name"))
        for x in m.methods_of(t):
            bump(adapt.resolve_method_return(m, adapt.method_return_key(m, x)))
            for pt in adapt.resolve_param_types(m, x):
                bump(pt)
        bump(adapt.parent_name(m, t))

        return {
            "found": True,
            "class": t.nested_name,
            "parent": self._resolve_parent(t.index),
            "referenced_types": sorted(
                ({"type": k, "count": v} for k, v in referenced.items()),
                key=lambda x: -x["count"],
            )[:100],
            "referenced_count": len(referenced),
            "note": "基于字段/方法签名的近似依赖，非精确调用图",
        }

    # ------------------------------------------------------ 结果句柄

    def read_query_result(self, handle: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """翻页读取之前某次查询的完整结果。"""
        qr = self.results.get(handle)
        if qr is None:
            return {
                "found": False,
                "handle": handle,
                "available_handles": self.results.handles(),
            }
        out = qr.page(offset, limit)
        out["found"] = True
        return out

    def search_query_result(
        self, handle: str, pattern: str, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """在之前某次查询的结果内部再过滤。"""
        qr = self.results.get(handle)
        if qr is None:
            return {
                "found": False,
                "handle": handle,
                "available_handles": self.results.handles(),
            }
        out = qr.search(pattern, offset, limit)
        out["found"] = True
        return out
