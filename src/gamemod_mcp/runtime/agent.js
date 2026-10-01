'use strict';
/**
 * GameMod MCP —— 游戏内运行时探针（注入脚本）
 * ============================================================================
 * 职责：在目标进程内部解析 il2cpp 导出符号，把「静态元数据」与「运行时实况」
 *       对上：
 *         · 字段的真实运行时偏移（静态元数据里没有，泛型实例化后才确定）
 *         · 方法的真实代码地址 → 换算成 RVA → 这就是 Hook 落点
 *         · 类/方法/字段的运行时枚举与类型名
 *         · 内存读写、AOB 扫描、任意 JS 求值（兜底手段）
 *
 * 设计原则（与 AiMod 的「元数据优先」一致）：
 *   1. 优先走官方 il2cpp_api.h 导出的稳定接口，不硬编码 Il2CppClass 内部布局。
 *      内部布局随 Unity 版本变化，硬编码是崩溃与错值的根源。
 *      仅在导出缺失时才回退到读结构体，并**明确标注回退已启用**。
 *   2. 任何一步失败都返回结构化错误，绝不返回看似成功的假数据。
 *   3. 所有对外返回的值必须是可 JSON 序列化的。
 *
 * 未经验证的部分（诚实声明）：
 *   · 本脚本的导出符号名取自 Unity 官方 il2cpp-api.h；不同 Unity 版本可能
 *     缺少部分导出（例如 il2cpp_field_get_offset 在个别版本被内联）。
 *     脚本会如实报告哪些 API 可用，不使用不可用 API。
 *   · 回退路径（读 Il2CppClass / FieldInfo 结构体偏移）取自社区通用布局，
 *     **未经本项目地面真值验证**，仅在导出缺失时启用并会在结果里标注。
 * ============================================================================
 */
(function () {

    // ---------------------------------------------------------------- 常量

    // 各平台 IL2CPP 运行时模块名
    var MODULE_CANDIDATES = [
        'GameAssembly.dll',
        'libil2cpp.so',
        'libil2cpp.dylib'
    ];

    // 官方 il2cpp-api.h 导出符号（按用途分组，便于缺失时给出可读报告）
    var API_NAMES = [
        // 域 / 线程 / GC
        'il2cpp_domain_get',
        'il2cpp_domain_get_assemblies',
        'il2cpp_thread_attach',
        'il2cpp_thread_detach',
        'il2cpp_gc_disable',
        'il2cpp_gc_enable',
        'il2cpp_gc_collect',
        // 程序集 / 映像
        'il2cpp_assembly_get_image',
        'il2cpp_image_get_name',
        'il2cpp_image_get_filename',
        'il2cpp_image_get_class_count',
        'il2cpp_image_get_class',
        // 类
        'il2cpp_class_from_name',
        'il2cpp_class_get_name',
        'il2cpp_class_get_namespace',
        'il2cpp_class_get_parent',
        'il2cpp_class_get_declaring_type',
        'il2cpp_class_get_image',
        'il2cpp_class_get_flags',
        'il2cpp_class_get_type',
        'il2cpp_class_get_interfaces',
        'il2cpp_class_get_nested_types',
        'il2cpp_class_instance_size',
        'il2cpp_class_value_size',
        'il2cpp_class_is_enum',
        'il2cpp_class_is_valuetype',
        'il2cpp_class_is_interface',
        'il2cpp_class_is_abstract',
        'il2cpp_class_is_generic',
        // 字段
        'il2cpp_class_get_fields',
        'il2cpp_field_get_name',
        'il2cpp_field_get_offset',
        'il2cpp_field_get_type',
        'il2cpp_field_get_parent',
        'il2cpp_field_get_flags',
        'il2cpp_field_static_get_value',
        'il2cpp_field_get_value',
        'il2cpp_field_set_value',
        // 方法
        'il2cpp_class_get_methods',
        'il2cpp_class_get_method_from_name',
        'il2cpp_method_get_name',
        'il2cpp_method_get_param_count',
        'il2cpp_method_get_param',
        'il2cpp_method_get_param_name',
        'il2cpp_method_get_return_type',
        'il2cpp_method_get_declaring_type',
        'il2cpp_method_is_instance',
        'il2cpp_method_is_generic',
        'il2cpp_method_is_inflated',
        // 类型
        'il2cpp_type_get_name',
        'il2cpp_type_get_object',
        'il2cpp_free',
        // 对象 / 字符串
        'il2cpp_object_new',
        'il2cpp_string_new',
        'il2cpp_string_length',
        'il2cpp_string_chars',
        // 调用
        'il2cpp_runtime_invoke',
        'il2cpp_resolve_icall',
        // 元数据
        'il2cpp_method_get_token',
        'il2cpp_class_get_type_token'
    ];

    // Il2CppString 布局（64 位）：
    //   Il2CppObject { Il2CppClass* klass; void* monitor; } = 16 字节
    //   int32_t length;   @ +0x10
    //   uint16_t chars[]; @ +0x14（UTF-16LE）
    var IL2CPP_STRING_LENGTH_OFFSET = 0x10;
    var IL2CPP_STRING_CHARS_OFFSET = 0x14;

    // 回退布局（未经验证，仅在官方导出缺失时启用）
    //   FieldInfo { const char* name; const Il2CppType* type; Il2CppClass* parent;
    //               int32_t offset; uint32_t token; }
    var FIELDINFO_OFFSET_FALLBACK = 0x18;
    //   MethodInfo { Il2CppMethodPointer methodPointer; ... }
    var METHODINFO_POINTER_OFFSET = 0x00;

    // ---------------------------------------------------------------- 运行时状态

    var mod = null;          // IL2CPP 运行时模块
    var api = {};            // 已解析的导出（缺失为 null）
    var apiMissing = [];     // 缺失的导出名
    var funcs = {};          // NativeFunction 包装
    var fallbacks = [];      // 已启用的回退路径说明
    var hooks = {};          // hookId -> listener
    var scratch = [];        // 持久缓冲区：防止 Memory.alloc 被 GC 回收
    var hookSeq = 0;
    var hookCounts = {};     // hookId -> 已触发次数
    var gcDisabled = false;
    var attached = false;

    // ---------------------------------------------------------------- 基础工具

    function fail(msg, extra) {
        var out = { ok: false, error: String(msg) };
        if (extra) { out.detail = extra; }
        return out;
    }

    /**
     * 统一把整数格式化成固定宽度的十六进制串。
     *
     * 为什么要固定宽度：token 是元数据里的「表号 + 行号」（如方法表 0x06），
     * 静态侧格式化成 0x060005d5、运行时若格式化成 0x60005d5，
     * 字符串比较就会误判为不一致。固定 8 位宽可避免这类假阴性。
     */
    function hex32(v, width) {
        var w = width || 8;
        var s = v.toString(16);
        while (s.length < w) { s = '0' + s; }
        return '0x' + s;
    }

    /** 安全读 C 字符串，空指针返回 null 而不是抛异常。 */
    function cstr(p) {
        if (p === null || p === undefined) { return null; }
        try {
            var q = ptr(p);
            if (q.isNull()) { return null; }
            return q.readUtf8String();
        } catch (e) {
            return null;
        }
    }

    /** Il2CppString* → JS 字符串。 */
    function il2cppString(p) {
        if (p === null || p === undefined) { return null; }
        try {
            var q = ptr(p);
            if (q.isNull()) { return null; }
            var len = q.add(IL2CPP_STRING_LENGTH_OFFSET).readS32();
            if (len < 0 || len > 0x100000) { return null; }   // 明显不合理
            return q.add(IL2CPP_STRING_CHARS_OFFSET).readUtf16String(len);
        } catch (e) {
            return null;
        }
    }

    /** 调 il2cpp_type_get_name，并释放返回的堆字符串（避免泄漏）。 */
    function typeName(typePtr) {
        if (typePtr === null || typePtr === undefined) { return null; }
        if (!funcs.il2cpp_type_get_name) { return null; }
        try {
            var p = funcs.il2cpp_type_get_name(ptr(typePtr));
            if (p.isNull()) { return null; }
            var s = p.readUtf8String();
            if (funcs.il2cpp_free) {
                try { funcs.il2cpp_free(p); } catch (e) { /* 释放失败不影响结果 */ }
            }
            return s;
        } catch (e) {
            return null;
        }
    }

    /** 取类名（优先导出，缺失时回退读结构体并登记）。 */
    function className(klass) {
        if (funcs.il2cpp_class_get_name) {
            return cstr(funcs.il2cpp_class_get_name(klass));
        }
        return null;
    }

    function classNamespace(klass) {
        if (funcs.il2cpp_class_get_namespace) {
            return cstr(funcs.il2cpp_class_get_namespace(klass));
        }
        return null;
    }

    /** 由类拼出完整名（嵌套类型会补上外层链）。 */
    function fullNameOf(klass) {
        return nestedFullName(klass);
    }

    /**
     * 拼出含嵌套外层的完整名。
     *
     * 为什么必须走 declaring_type：IL2CPP 里 `namespace` 只属于**最外层**类型，
     * 嵌套类型的 namespace 是空的、名字只有短名。实测样本 20,072 个类型里
     * 7,056 个（35%）是嵌套类型，131 个叫 `State` 的类型只有 1 个是顶层 ——
     * 直接 `ns + '.' + name` 会把 130 个 `State` 全拼成同一个错名。
     *
     * 自底向上收集短名，遇到第一个非空 namespace 就补上并停止。
     * `il2cpp_class_get_declaring_type` 缺失时退化为原来的行为。
     */
    function nestedFullName(klass) {
        var parts = [];
        var cur = klass;
        var guard = 0;
        while (cur && !cur.isNull() && guard++ < 32) {
            var nm = className(cur);
            if (nm) { parts.unshift(nm); }
            var ns = classNamespace(cur);
            if (ns && ns.length) { parts.unshift(ns); break; }
            if (!funcs.il2cpp_class_get_declaring_type) { break; }
            var outer;
            try { outer = funcs.il2cpp_class_get_declaring_type(cur); }
            catch (e) { break; }
            if (!outer || outer.isNull()) { break; }
            cur = outer;
        }
        return parts.length ? parts.join('.') : null;
    }

    /** 把绝对地址换算成「模块内 RVA + 十六进制」——Hook 落点就用这个。 */
    function describeAddress(addr) {
        var q = ptr(addr);
        if (q.isNull()) { return null; }
        var out = { address: q.toString() };
        if (mod) {
            var rva = q.sub(mod.base);
            out.rva = rva.toString();
            out.rva_hex = hex32(rva);
            out.module = mod.name;
        }
        return out;
    }

    /** 判断地址是否落在 IL2CPP 运行时模块内（区分托管方法与 icall/转发桩）。 */
    function isInModule(addr) {
        if (!mod) { return false; }
        var q = ptr(addr);
        return q.compare(mod.base) >= 0 && q.compare(mod.base.add(mod.size)) < 0;
    }

    // ---------------------------------------------------------------- 模块与导出解析

    function resolveModule(preferred) {
        var names = preferred ? [preferred].concat(MODULE_CANDIDATES) : MODULE_CANDIDATES;
        for (var i = 0; i < names.length; i++) {
            var m = Process.findModuleByName(names[i]);
            if (m) { return m; }
        }
        return null;
    }

    function resolveExports() {
        api = {};
        apiMissing = [];
        for (var i = 0; i < API_NAMES.length; i++) {
            var nm = API_NAMES[i];
            var p = null;
            try {
                if (mod) { p = mod.findExportByName(nm); }
            } catch (e) {
                p = null;
            }
            if (p) { api[nm] = p; } else { apiMissing.push(nm); }
        }
    }

    /**
     * 用 NativeFunction 逐个包装。
     * 返回类型统一用 'pointer'：C 的 int32 返回值从 x0/w0 返回，
     * 低 32 位即为有效值，调用方按需截断，避免各函数签名不一致带来的错误。
     */
    function buildFunctions() {
        funcs = {};
        var sigs = {
            il2cpp_domain_get: ['pointer', []],
            il2cpp_domain_get_assemblies: ['pointer', ['pointer', 'pointer']],
            il2cpp_thread_attach: ['pointer', ['pointer']],
            il2cpp_thread_detach: ['void', ['pointer']],
            il2cpp_gc_disable: ['void', []],
            il2cpp_gc_enable: ['void', []],
            il2cpp_gc_collect: ['void', ['int']],

            il2cpp_assembly_get_image: ['pointer', ['pointer']],
            il2cpp_image_get_name: ['pointer', ['pointer']],
            il2cpp_image_get_filename: ['pointer', ['pointer']],
            il2cpp_image_get_class_count: ['pointer', ['pointer']],
            // 注意：第二个参数是**序号 size_t**，不是指针。写成 'pointer'
            // 会直接抛 "expected a pointer"（已实测）。
            il2cpp_image_get_class: ['pointer', ['pointer', 'size_t']],

            il2cpp_class_from_name: ['pointer', ['pointer', 'pointer', 'pointer']],
            il2cpp_class_get_name: ['pointer', ['pointer']],
            il2cpp_class_get_namespace: ['pointer', ['pointer']],
            il2cpp_class_get_parent: ['pointer', ['pointer']],
            // 嵌套类型的**外层类**。别和 il2cpp_class_get_parent（基类）搞混：
            // 对 MonoBehaviour 的子类，get_parent 给基类，
            // get_declaring_type 给外层嵌套类（顶层类为 NULL）。
            il2cpp_class_get_declaring_type: ['pointer', ['pointer']],
            il2cpp_class_get_image: ['pointer', ['pointer']],
            il2cpp_class_get_flags: ['pointer', ['pointer']],
            il2cpp_class_get_type: ['pointer', ['pointer']],
            il2cpp_class_get_interfaces: ['pointer', ['pointer', 'pointer']],
            il2cpp_class_get_nested_types: ['pointer', ['pointer', 'pointer']],
            il2cpp_class_instance_size: ['pointer', ['pointer']],
            il2cpp_class_value_size: ['pointer', ['pointer', 'pointer']],
            il2cpp_class_is_enum: ['pointer', ['pointer']],
            il2cpp_class_is_valuetype: ['pointer', ['pointer']],
            il2cpp_class_is_interface: ['pointer', ['pointer']],
            il2cpp_class_is_abstract: ['pointer', ['pointer']],
            il2cpp_class_is_generic: ['pointer', ['pointer']],

            il2cpp_class_get_fields: ['pointer', ['pointer', 'pointer']],
            il2cpp_field_get_name: ['pointer', ['pointer']],
            il2cpp_field_get_offset: ['pointer', ['pointer']],
            il2cpp_field_get_type: ['pointer', ['pointer']],
            il2cpp_field_get_parent: ['pointer', ['pointer']],
            il2cpp_field_get_flags: ['pointer', ['pointer']],
            il2cpp_field_static_get_value: ['void', ['pointer', 'pointer']],
            il2cpp_field_get_value: ['void', ['pointer', 'pointer', 'pointer']],
            il2cpp_field_set_value: ['void', ['pointer', 'pointer', 'pointer']],

            il2cpp_class_get_methods: ['pointer', ['pointer', 'pointer']],
            il2cpp_class_get_method_from_name: ['pointer', ['pointer', 'pointer', 'pointer']],
            il2cpp_method_get_name: ['pointer', ['pointer']],
            il2cpp_method_get_param_count: ['pointer', ['pointer']],
            il2cpp_method_get_param: ['pointer', ['pointer', 'pointer']],
            il2cpp_method_get_param_name: ['pointer', ['pointer', 'pointer']],
            il2cpp_method_get_return_type: ['pointer', ['pointer']],
            il2cpp_method_get_declaring_type: ['pointer', ['pointer']],
            il2cpp_method_is_instance: ['pointer', ['pointer']],
            il2cpp_method_is_generic: ['pointer', ['pointer']],
            il2cpp_method_is_inflated: ['pointer', ['pointer']],

            il2cpp_type_get_name: ['pointer', ['pointer']],
            il2cpp_type_get_object: ['pointer', ['pointer']],
            il2cpp_free: ['void', ['pointer']],

            il2cpp_object_new: ['pointer', ['pointer']],
            il2cpp_string_new: ['pointer', ['pointer']],
            il2cpp_string_length: ['pointer', ['pointer']],
            il2cpp_string_chars: ['pointer', ['pointer']],

            il2cpp_runtime_invoke: ['pointer', ['pointer', 'pointer', 'pointer', 'pointer']],
            il2cpp_resolve_icall: ['pointer', ['pointer']],

            il2cpp_method_get_token: ['pointer', ['pointer']],
            il2cpp_class_get_type_token: ['pointer', ['pointer']]
        };

        for (var nm in sigs) {
            if (!Object.prototype.hasOwnProperty.call(sigs, nm)) { continue; }
            if (!api[nm]) { continue; }
            try {
                funcs[nm] = new NativeFunction(api[nm], sigs[nm][0], sigs[nm][1]);
            } catch (e) {
                // 包装失败（少见）：当作不可用，并记录原因
                apiMissing.push(nm + '(wrap失败:' + e.message + ')');
            }
        }
    }

    /** 首次调用时初始化：找模块、解析导出、包装函数、附着线程。 */
    function ensureReady(preferredModule) {
        if (attached) { return { ok: true, already: true }; }

        mod = resolveModule(preferredModule);
        if (!mod) {
            return fail('未找到 IL2CPP 运行时模块（已尝试 ' +
                        MODULE_CANDIDATES.join(' / ') + '）',
                        { tried: MODULE_CANDIDATES, process: Process.id });
        }

        resolveExports();
        buildFunctions();
        // 重新绑定导出后，之前按旧函数指针建的类索引必须作废
        classIndex = null;

        if (!funcs.il2cpp_domain_get) {
            return fail('模块存在但缺少 il2cpp_domain_get 导出，' +
                        '该游戏可能不是 IL2CPP 构建，或符号已被剥离',
                        { module: mod.name, missing: apiMissing });
        }

        // 附着当前 Frida 线程到 GC 域，否则调用 il2cpp 接口可能崩溃
        if (funcs.il2cpp_thread_attach) {
            try {
                var domain = funcs.il2cpp_domain_get();
                funcs.il2cpp_thread_attach(domain);
            } catch (e) {
                fallbacks.push('il2cpp_thread_attach 调用失败：' + e.message);
            }
        }

        // 字段偏移导出缺失时启用回退（明确登记，结果里会标注）
        if (!funcs.il2cpp_field_get_offset) {
            fallbacks.push(
                'il2cpp_field_get_offset 不可用，字段偏移改用 FieldInfo+' +
                FIELDINFO_OFFSET_FALLBACK.toString(16) + ' 回退读取（未经地面真值验证）'
            );
        }

        attached = true;
        return {
            ok: true,
            module: mod.name,
            base: mod.base.toString(),
            size: mod.size,
            api_available: Object.keys(funcs).length,
            api_missing: apiMissing,
            fallbacks: fallbacks
        };
    }

    // ---------------------------------------------------------------- 域与映像

    function allImages() {
        var domain = funcs.il2cpp_domain_get();
        var countPtr = Memory.alloc(Process.pointerSize);
        countPtr.writeU32(0);
        var assemblies = funcs.il2cpp_domain_get_assemblies(domain, countPtr);
        var count = countPtr.readU32();
        var out = [];
        for (var i = 0; i < count; i++) {
            var asm = assemblies.add(i * Process.pointerSize).readPointer();
            if (asm.isNull()) { continue; }
            var img = funcs.il2cpp_assembly_get_image
                ? funcs.il2cpp_assembly_get_image(asm) : null;
            if (!img || img.isNull()) { continue; }
            var rec = { image: img };
            if (funcs.il2cpp_image_get_name) {
                rec.name = cstr(funcs.il2cpp_image_get_name(img));
            }
            if (funcs.il2cpp_image_get_filename) {
                rec.filename = cstr(funcs.il2cpp_image_get_filename(img));
            }
            if (funcs.il2cpp_image_get_class_count) {
                rec.class_count = funcs.il2cpp_image_get_class_count(img).toInt32();
            }
            out.push(rec);
        }
        return out;
    }

    function findImage(name) {
        var imgs = allImages();
        var needle = String(name).toLowerCase();
        for (var i = 0; i < imgs.length; i++) {
            var n = (imgs[i].name || '').toLowerCase();
            var f = (imgs[i].filename || '').toLowerCase();
            if (n === needle || f === needle || n.indexOf(needle) >= 0 ||
                f.indexOf(needle) >= 0) {
                return imgs[i];
            }
        }
        return null;
    }

    // ---------------------------------------------------------------- 类 / 字段 / 方法

    /**
     * 运行时类索引（惰性构建一次，之后复用）。
     *
     * 为什么不能只用 `il2cpp_class_from_name`：那个 API **只查得到顶层类型**，
     * 嵌套类型一个都拿不到。实测样本（Garden of Witches）：
     *   · 20,072 个类型里 7,056 个（35%）是嵌套类型；
     *   · 名为 `State` 的类型有 131 个，其中**只有 1 个是顶层**；
     *   · 在 `TeamTapas.ProjectR.dll` 里遍历出 118 个 `State`，
     *     用 `il2cpp_class_from_name` 逐个回查 —— **命中 0 个、漏掉 118 个**。
     * 也就是说「嵌套类在运行时根本查不到」，而 Mod 最常 Hook 的
     * 恰恰是这类内部类（状态机、协程体、私有嵌套类）。
     *
     * 全量枚举实测 94 个映像 / 20,072 个类 / 119 ms，一次构建即可长期复用。
     */
    var classIndex = null;

    function buildClassIndex() {
        if (!funcs.il2cpp_image_get_class_count || !funcs.il2cpp_image_get_class) {
            return null;   // 缺 API，调用方退化为 from_name 快路径
        }
        var byFull = {};
        var byShort = {};
        var imgs = allImages();
        var n = 0;
        for (var i = 0; i < imgs.length; i++) {
            var cnt;
            try {
                cnt = funcs.il2cpp_image_get_class_count(
                    imgs[i].image).toInt32();
            } catch (e) { continue; }
            for (var j = 0; j < cnt; j++) {
                var k;
                try { k = funcs.il2cpp_image_get_class(imgs[i].image, j); }
                catch (e) { continue; }
                if (!k || k.isNull()) { continue; }
                var shortName = className(k);
                if (!shortName) { continue; }
                var ent = {
                    klass: k,
                    image: imgs[i],
                    name: shortName,
                    namespace: classNamespace(k) || '',
                    full_name: nestedFullName(k)
                };
                if (ent.full_name) {
                    if (!byFull[ent.full_name]) { byFull[ent.full_name] = []; }
                    byFull[ent.full_name].push(ent);
                }
                if (!byShort[shortName]) { byShort[shortName] = []; }
                byShort[shortName].push(ent);
                n++;
            }
        }
        return { byFull: byFull, byShort: byShort, count: n };
    }

    function classIndexOf() {
        if (classIndex === null) { classIndex = buildClassIndex(); }
        return classIndex;
    }

    /**
     * 解析类。返回以下之一：
     *   {ok: true,  klass, image, entry}
     *   {ok: false, not_found: true}
     *   {ok: false, ambiguous: true, candidates: [...], candidate_count, want_full}
     *
     * **歧义一律拒绝**，绝不「取第一个」。理由与静态侧同名类消歧同源：
     * 挂到错误的重载/类上，Hook 照样会成功，错误要到很久以后才以
     * 「数据不对」的形式暴露 —— 那比直接报错危险得多。
     */
    function resolveClass(imageName, namespace, name) {
        var img = null;
        if (imageName) {
            img = findImage(imageName);
            if (!img) {
                return { ok: false, error: '未找到映像 ' + imageName };
            }
        }
        var wantFull = namespace ? (namespace + '.' + name) : String(name);
        var idx = classIndexOf();
        if (!idx) {
            // 退化路径：只有 from_name 可用（顶层类型 + 第一个命中）
            var imgs0 = img ? [img] : allImages();
            var nsBuf = Memory.allocUtf8String(namespace || '');
            var nmBuf = Memory.allocUtf8String(name);
            for (var i0 = 0; i0 < imgs0.length; i0++) {
                var k0 = funcs.il2cpp_class_from_name(imgs0[i0].image, nsBuf,
                                                     nmBuf);
                if (k0 && !k0.isNull()) {
                    return { ok: true, klass: k0, image: imgs0[i0],
                             degraded: '缺少 il2cpp_image_get_class，' +
                                       '嵌套类型无法定位' };
                }
            }
            return { ok: false, not_found: true };
        }

        // 候选来源两处，都要看：
        //   · byShort[name] —— 调用方给的是「命名空间 + 短名」（静态索引的口径）
        //   · byFull[wantFull] —— 调用方直接给了含嵌套外层的完整名
        var pool = (idx.byShort[name] || []).concat(idx.byFull[wantFull] || []);
        var seen = {};
        var hits = [];
        for (var i = 0; i < pool.length; i++) {
            var e = pool[i];
            if (img && e.image.name !== img.name) { continue; }
            // 命中条件：完整名相等，或（命名空间 + 短名）相等
            var byFullName = (e.full_name === wantFull);
            var byNsShort = (e.namespace === (namespace || '') &&
                             e.name === String(name));
            if (!byFullName && !byNsShort) { continue; }
            var key = e.full_name + '@' + e.image.name;
            if (seen[key]) { continue; }
            seen[key] = 1;
            hits.push(e);
        }
        if (!hits.length) { return { ok: false, not_found: true }; }
        if (hits.length > 1) {
            return {
                ok: false,
                ambiguous: true,
                candidate_count: hits.length,
                want_full: wantFull,
                candidates: hits.slice(0, 30).map(function (e) {
                    return {
                        full_name: e.full_name,
                        name: e.name,
                        namespace: e.namespace,
                        image: e.image.name
                    };
                }),
                candidates_truncated: hits.length > 30
            };
        }
        return { ok: true, klass: hits[0].klass, image: hits[0].image,
                 entry: hits[0] };
    }

    /** 把 resolveClass 的失败转成统一的可操作错误返回。 */
    function resolveClassOrFail(imageName, namespace, name) {
        var r = resolveClass(imageName, namespace, name);
        if (r.ok) { return r; }
        var label = (namespace ? namespace + '.' : '') + name;
        if (r.error) { return fail(r.error, { image: imageName }); }
        if (r.ambiguous) {
            // 候选与计数**平铺在顶层**（不用 fail 的 detail 包裹）：
            // 静态侧 class_detail/search_fields 的同类拒绝就是这个形状，
            // 两侧返回结构一致，Agent 才不用按工具记两套读法。
            return {
                ok: false,
                error: '类名有歧义：' + label + ' 在运行时匹配到 ' +
                       r.candidate_count + ' 个类。请用 image= 或' +
                       '**完整嵌套名**（candidates[].full_name）指定唯一目标。',
                ambiguous: true,
                candidate_count: r.candidate_count,
                want_full: r.want_full,
                candidates: r.candidates,
                candidates_truncated: r.candidates_truncated,
                hint: '嵌套类型（如 Xxx.State）在静态索引里 namespace 为空，' +
                      '要定位它请把 name 传成完整的嵌套名。'
            };
        }
        return fail('未找到类 ' + label, { image: imageName || '(全部映像)' });
    }

    function fieldOffset(fieldInfo) {
        if (funcs.il2cpp_field_get_offset) {
            return { offset: funcs.il2cpp_field_get_offset(fieldInfo).toInt32(),
                     via: 'il2cpp_field_get_offset' };
        }
        // 回退：读 FieldInfo 结构体的 offset 成员
        try {
            return {
                offset: fieldInfo.add(FIELDINFO_OFFSET_FALLBACK).readS32(),
                via: 'FieldInfo+' + FIELDINFO_OFFSET_FALLBACK.toString(16) + '(回退)'
            };
        } catch (e) {
            return { offset: null, via: null, error: e.message };
        }
    }

    /** 枚举类的字段，带运行时偏移与类型名。 */
    function fieldsOf(klass, wantStatic) {
        if (!funcs.il2cpp_class_get_fields) { return []; }
        var iter = Memory.alloc(Process.pointerSize);
        iter.writePointer(NULL);
        var out = [];
        var guard = 0;
        for (;;) {
            var f = funcs.il2cpp_class_get_fields(klass, iter);
            if (!f || f.isNull()) { break; }
            if (++guard > 100000) { break; }   // 防御性上限
            var rec = {
                name: cstr(funcs.il2cpp_field_get_name(f)),
                type: funcs.il2cpp_field_get_type
                    ? typeName(funcs.il2cpp_field_get_type(f)) : null,
                token: null
            };
            var off = fieldOffset(f);
            rec.offset = off.offset;
            rec.offset_via = off.via;
            if (funcs.il2cpp_field_get_flags) {
                rec.flags = hex32(funcs.il2cpp_field_get_flags(f).toUInt32());
            }
            // 静态字段没有实例偏移语义，用 flag 区分（0x10 = FIELD_ATTRIBUTE_STATIC）
            rec.is_static = rec.flags
                ? ((parseInt(rec.flags, 16) & 0x10) !== 0) : null;
            if (wantStatic === true && rec.is_static !== true) { continue; }
            if (wantStatic === false && rec.is_static === true) { continue; }
            out.push(rec);
        }
        return out;
    }

    /** 取 MethodInfo 的原生代码地址。 */
    function methodPointer(methodInfo) {
        try {
            return methodInfo.readPointer();
        } catch (e) {
            return null;
        }
    }

    /** 枚举类的方法，带真实代码地址 / RVA。 */
    function methodsOf(klass) {
        if (!funcs.il2cpp_class_get_methods) { return []; }
        var iter = Memory.alloc(Process.pointerSize);
        iter.writePointer(NULL);
        var out = [];
        var guard = 0;
        for (;;) {
            var m = funcs.il2cpp_class_get_methods(klass, iter);
            if (!m || m.isNull()) { break; }
            if (++guard > 100000) { break; }
            var mp = methodPointer(m);
            var rec = {
                name: cstr(funcs.il2cpp_method_get_name(m)),
                param_count: funcs.il2cpp_method_get_param_count
                    ? funcs.il2cpp_method_get_param_count(m).toInt32() : null,
                return_type: funcs.il2cpp_method_get_return_type
                    ? typeName(funcs.il2cpp_method_get_return_type(m)) : null,
                is_instance: funcs.il2cpp_method_is_instance
                    ? (funcs.il2cpp_method_is_instance(m).toInt32() !== 0) : null,
                is_generic: funcs.il2cpp_method_is_generic
                    ? (funcs.il2cpp_method_is_generic(m).toInt32() !== 0) : null,
                token: funcs.il2cpp_method_get_token
                    ? hex32(funcs.il2cpp_method_get_token(m).toUInt32())
                    : null,
                method_info: m.toString()
            };
            if (mp && !mp.isNull()) {
                var d = describeAddress(mp);
                rec.pointer = d.address;
                rec.rva = d.rva;
                rec.rva_hex = d.rva_hex;
                rec.in_module = isInModule(mp);
            } else {
                rec.pointer = null;
                rec.note = '方法未获得原生代码地址（可能是泛型定义或抽象方法）';
            }
            out.push(rec);
        }
        return out;
    }

    /** 参数名列表（失败返回空数组，不抛异常）。 */
    function paramNames(methodInfo) {
        if (!funcs.il2cpp_method_get_param_name || !funcs.il2cpp_method_get_param_count) {
            return [];
        }
        var n = funcs.il2cpp_method_get_param_count(methodInfo).toInt32();
        var out = [];
        for (var i = 0; i < n; i++) {
            out.push(cstr(funcs.il2cpp_method_get_param_name(methodInfo, i)));
        }
        return out;
    }

    // ---------------------------------------------------------------- Hook

    /**
     * 安全地取第 i 个整型寄存器参数。
     *
     * 为什么不能写 `if (i >= args.length) break;`：Frida 17 起 `args` 是
     * **寄存器视图**，根本不暴露 `length` —— 读 `args.length` 会直接抛
     * `RangeError: invalid array index`。老代码的判界语句自己就是异常源，
     * 于是任何带 `args_type` 的 Hook 在 onEnter 里就抛异常，
     * **整条 enter 事件（含 from / thread / args）全部丢失**，只剩 leave，
     * 调用方会误以为「参数没捕获到」而不是「探针坏了」。
     * 所以越界只能一个一个试，失败即视为「没有这个参数」。
     */
    function safeArg(args, i) {
        try {
            var v = args[i];
            return (v === undefined) ? null : v;
        } catch (e) {
            return null;
        }
    }

    /**
     * 从 XMM 寄存器里读浮点值。
     *
     * x64 的浮点参数/返回值**不走** RCX/RDX/R8/R9：`args[i]` 拿到的是与
     * 该浮点参数无关的整型寄存器（实测 Mathf.Abs(-2.5f) 的 args[0]
     * 恰好是 RCX 的残留值）。Frida 17 把 xmm 暴露成 16 字节 ArrayBuffer，
     * 更老的版本是 `{f: [...], d: [...]}`，这里两种都认。
     */
    function xmmValue(ctx, index, kind) {
        try {
            if (!ctx) { return null; }
            var reg = ctx['xmm' + index];
            if (reg === undefined || reg === null) { return null; }
            if (reg.f && kind === 'float' && reg.f.length > 0) { return reg.f[0]; }
            if (reg.d && kind === 'double' && reg.d.length > 0) { return reg.d[0]; }
            var buf = null, off = 0;
            if (reg instanceof ArrayBuffer) {
                buf = reg;
            } else if (typeof reg.byteLength === 'number') {
                off = reg.byteOffset || 0;
                buf = reg.buffer;
            }
            var need = (kind === 'double') ? 8 : 4;
            if (!buf || buf.byteLength < off + need) { return null; }
            var dv = new DataView(buf, off);
            return (kind === 'double') ? dv.getFloat64(0, true) : dv.getFloat32(0, true);
        } catch (e) {
            return null;
        }
    }

    /**
     * 按参数规格从整型寄存器读值。
     * 规格字符串：int / uint / long / ulong / bool / ptr / string / cstring
     *
     * float / double **不经过这里**：x64 的浮点参数与返回值走 XMM 寄存器，
     * 一律由 xmmValue 处理。这里对 fp 规格显式报错，避免路由漏了之后
     * 静默按指针去读内存（那会读出垃圾或直接崩）。
     */
    function readArg(spec, raw) {
        try {
            switch (spec) {
                case 'int':     return raw.toInt32();
                case 'uint':    return raw.toUInt32();
                case 'long':    return raw.toInt64().toString();
                case 'ulong':   return raw.toUInt64().toString();
                case 'bool':    return raw.toInt32() !== 0;
                case 'float':
                case 'double':  throw new Error('浮点参数/返回值必须走 xmmValue');
                case 'ptr':     return raw.toString();
                case 'cstring': return cstr(raw);
                case 'string':  return il2cppString(raw);
                default:        return raw.toString();
            }
        } catch (e) {
            return '<读取失败:' + e.message + '>';
        }
    }

    function hookAt(spec) {
        var target;
        try {
            target = ptr(spec.address);
        } catch (e) {
            return fail('地址无法解析：' + spec.address);
        }

        var id = 'h' + (++hookSeq);
        var argSpecs = spec.args || [];
        var maxCalls = typeof spec.max_calls === 'number' ? spec.max_calls : 200;
        var wantBacktrace = !!spec.backtrace;
        var wantRetval = spec.retval !== false;      // 默认捕获返回值
        var retvalSpec = spec.retval_type || 'ptr';
        var counts = { enter: 0, leave: 0 };
        hookCounts[id] = counts;

        try {
            var listener = Interceptor.attach(target, {
                onEnter: function (args) {
                    if (counts.enter >= maxCalls) { return; }
                    counts.enter++;
                    var rec = {
                        type: 'hook', id: id, label: spec.label || null,
                        phase: 'enter',
                        seq: counts.enter,
                        thread: this.threadId,
                        from: describeAddress(this.returnAddress)
                    };
                    if (spec.this_type === 'string') {
                        var selfPtr = safeArg(args, 0);
                        if (selfPtr !== null) { rec.this_str = il2cppString(selfPtr); }
                    }
                    // 整型参数按 RCX/RDX/R8/R9 顺序取（`this` 占第一个），
                    // 浮点参数按 XMM0..3 顺序取 —— 两套计数器互不影响，
                    // 混用签名时不能拿同一个下标去查两处。
                    var vals = [];
                    var intIdx = 0, fpIdx = 0, argErr = null;
                    for (var i = 0; i < argSpecs.length; i++) {
                        var specI = argSpecs[i];
                        if (specI === 'float' || specI === 'double') {
                            var fv = xmmValue(this.context, fpIdx, specI);
                            fpIdx++;
                            if (fv === null) {
                                argErr = 'args[' + i + ']（' + specI + '）取不到：'
                                       + 'xmm' + (fpIdx - 1) + ' 不可读';
                                vals.push(null);
                                continue;
                            }
                            vals.push(fv);
                            continue;
                        }
                        var rawI = safeArg(args, intIdx);
                        intIdx++;
                        if (rawI === null) { break; }
                        try {
                            vals.push(readArg(specI, rawI));
                        } catch (e) {
                            argErr = 'args[' + i + '] 读取失败：' + e.message;
                            break;
                        }
                    }
                    rec.args = vals;
                    // 参数读取失败**不能**把整条 enter 事件吞掉：
                    // 调用方要的是「这次调用发生了、从哪来」，参数是附加值。
                    if (argErr) { rec.args_error = argErr; }
                    if (wantBacktrace) {
                        try {
                            rec.backtrace = Thread.backtrace(this.context,
                                Backtracer.ACCURATE).map(function (a) {
                                    var d = describeAddress(a);
                                    return d ? (d.module ? (d.module + '!' + d.rva_hex)
                                                         : d.address)
                                             : String(a);
                                });
                        } catch (e) {
                            rec.backtrace = ['<回溯失败:' + e.message + '>'];
                        }
                    }
                    try { send(rec); } catch (e) { /* 发送失败忽略，不阻断游戏 */ }
                },
                onLeave: function (retval) {
                    if (counts.leave >= maxCalls) { return; }
                    counts.leave++;
                    var rec = {
                        type: 'hook', id: id, label: spec.label || null,
                        phase: 'leave',
                        seq: counts.leave,
                        thread: this.threadId
                    };
                    if (wantRetval) {
                        // 浮点返回值在 XMM0，不在 RAX —— 按 ptr 读会给出一个
                        // 看着像地址、其实毫无意义的整数（实测 0x20da0a48360）。
                        if (retvalSpec === 'float' || retvalSpec === 'double') {
                            var rv = xmmValue(this.context, 0, retvalSpec);
                            rec.retval = (rv === null) ? null : rv;
                            if (rv === null) { rec.retval_error = 'xmm0 不可读'; }
                        } else {
                            rec.retval = readArg(retvalSpec, retval);
                        }
                    }
                    try { send(rec); } catch (e) { /* 同上 */ }
                }
            });
            hooks[id] = { listener: listener, spec: spec, target: target };
            return {
                ok: true,
                hook_id: id,
                target: target.toString(),
                target_rva: describeAddress(target),
                in_module: isInModule(target),
                args: argSpecs,
                label: spec.label || null,
                note: '事件通过 send() 推送到宿主，用 drain_events 读取'
            };
        } catch (e) {
            return fail('Hook 失败：' + e.message, { address: spec.address });
        }
    }

    // ---------------------------------------------------------------- 导出接口

    rpc.exports = {

        /** 健康检查：不依赖 IL2CPP，验证脚本已加载。 */
        ping: function () {
            return {
                ok: true,
                frida: Frida.version,
                runtime: Script.runtime,
                arch: Process.arch,
                pointer_size: Process.pointerSize,
                platform: Process.platform,
                pid: Process.id
            };
        },

        /** 初始化并报告模块 / 导出可用性。 */
        init: function (preferredModule) {
            return ensureReady(preferredModule);
        },

        /** 运行时总览：模块、域、映像数量。 */
        runtimeInfo: function (preferredModule) {
            var ready = ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            var imgs = [];
            try {
                imgs = allImages();
            } catch (e) {
                return fail('枚举映像失败：' + e.message);
            }
            return {
                ok: true,
                module: mod.name,
                base: mod.base.toString(),
                size: mod.size,
                image_count: imgs.length,
                images: imgs.map(function (x) {
                    return { name: x.name, class_count: x.class_count };
                }),
                api_available: Object.keys(funcs).length,
                api_missing: apiMissing,
                fallbacks: fallbacks,
                gc_disabled: gcDisabled
            };
        },

        /** 列出全部映像（程序集）。 */
        listImages: function (preferredModule) {
            var ready = ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            return { ok: true, images: allImages() };
        },

        /** 查类：返回运行时字段偏移与方法地址。 */
        classInfo: function (opts, preferredModule) {
            var ready = ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            opts = opts || {};
            var found = resolveClassOrFail(opts.image, opts.namespace, opts.name);
            if (!found.ok) { return found; }
            var k = found.klass;
            var out = {
                ok: true,
                image: found.image.name,
                full_name: fullNameOf(k),
                klass: k.toString()
            };
            if (funcs.il2cpp_class_get_parent) {
                var par = funcs.il2cpp_class_get_parent(k);
                out.parent = (par && !par.isNull()) ? fullNameOf(par) : null;
            }
            if (funcs.il2cpp_class_get_flags) {
                out.flags = hex32(funcs.il2cpp_class_get_flags(k).toUInt32());
            }
            if (funcs.il2cpp_class_is_enum) {
                out.is_enum = funcs.il2cpp_class_is_enum(k).toInt32() !== 0;
            }
            if (funcs.il2cpp_class_is_valuetype) {
                out.is_value_type = funcs.il2cpp_class_is_valuetype(k).toInt32() !== 0;
            }
            if (funcs.il2cpp_class_instance_size) {
                out.instance_size = funcs.il2cpp_class_instance_size(k).toInt32();
            }
            // 字段：默认给出实例字段的真实偏移
            if (opts.fields !== false) {
                out.fields = fieldsOf(k, null);
                out.field_count = out.fields.length;
            }
            // 方法：默认给出 RVA，这是 Hook 的落点
            if (opts.methods !== false) {
                var ms = methodsOf(k);
                // 支持按名字过滤，便于直接定位 Hook 目标
                if (opts.method_filter) {
                    var f = String(opts.method_filter);
                    ms = ms.filter(function (x) {
                        return x.name && x.name.indexOf(f) >= 0;
                    });
                }
                out.methods = ms;
                out.method_count = ms.length;
            }
            if (fallbacks.length) { out.fallbacks = fallbacks; }
            return out;
        },

        /** 只取某方法的 Hook 落点信息。 */
        methodTarget: function (opts, preferredModule) {
            var ready = ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            opts = opts || {};
            var found = resolveClassOrFail(opts.image, opts.namespace, opts.name);
            if (!found.ok) { return found; }
            var ms = methodsOf(found.klass);
            var hits = ms.filter(function (x) {
                return x.name === opts.method;
            });
            if (!hits.length) {
                return fail('未找到方法 ' + opts.method, {
                    available: ms.map(function (x) { return x.name; }).slice(0, 80)
                });
            }
            return {
                ok: true,
                class: fullNameOf(found.klass),
                image: found.image.name,
                module: mod.name,
                module_base: mod.base.toString(),
                candidates: hits,
                note: '用 candidates[].pointer 直接 Hook；' +
                      '或静态 RVA + module_base 定位'
            };
        },

        /** 枚举类的字段（可只要静态字段）。 */
        classFields: function (opts, preferredModule) {
            var ready = ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            opts = opts || {};
            var found = resolveClassOrFail(opts.image, opts.namespace, opts.name);
            if (!found.ok) { return found; }
            var want = (opts.static_only === true) ? true
                     : (opts.instance_only === true) ? false : null;
            var fs = fieldsOf(found.klass, want);
            return {
                ok: true,
                class: fullNameOf(found.klass),
                fields: fs,
                count: fs.length,
                fallbacks: fallbacks
            };
        },

        /**
         * 安装 Hook。
         *
         * 注意：本接口**不要求 IL2CPP 初始化成功**。给定绝对地址即可挂钩，
         * 与引擎无关 —— 原生游戏的函数、系统 API、甚至 Unity 之外的进程
         * 都能 Hook。若 IL2CPP 模块恰好可用，会顺带解析模块基址，
         * 以便把目标地址换算成 RVA（方便与静态元数据对照）。
         */
        hook: function (spec, preferredModule) {
            if (!mod) {
                // 尽力而为：解析不到就只按绝对地址挂钩，不阻断
                try { mod = resolveModule(preferredModule); } catch (e) { mod = null; }
            }
            return hookAt(spec || {});
        },

        /** 卸载指定 Hook。 */
        unhook: function (id) {
            var h = hooks[id];
            if (!h) { return fail('没有这个 Hook：' + id, { hooks: Object.keys(hooks) }); }
            try {
                h.listener.detach();
            } catch (e) {
                return fail('卸载失败：' + e.message);
            }
            delete hooks[id];
            delete hookCounts[id];
            return { ok: true, hook_id: id, removed: true };
        },

        /** 卸载全部 Hook。 */
        unhookAll: function () {
            var n = 0;
            for (var id in hooks) {
                if (!Object.prototype.hasOwnProperty.call(hooks, id)) { continue; }
                try { hooks[id].listener.detach(); n++; } catch (e) { /* 继续卸其它 */ }
            }
            hooks = {};
            hookCounts = {};
            return { ok: true, removed: n };
        },

        /** 当前 Hook 列表与触发计数。 */
        hookStatus: function () {
            var out = [];
            for (var id in hooks) {
                if (!Object.prototype.hasOwnProperty.call(hooks, id)) { continue; }
                out.push({
                    hook_id: id,
                    label: hooks[id].spec.label || null,
                    target: hooks[id].target.toString(),
                    target_rva: describeAddress(hooks[id].target),
                    enter_count: hookCounts[id] ? hookCounts[id].enter : 0,
                    leave_count: hookCounts[id] ? hookCounts[id].leave : 0
                });
            }
            return { ok: true, hooks: out, count: out.length };
        },

        /** 读内存。 */
        readMemory: function (address, size) {
            try {
                var p = ptr(address);
                var n = parseInt(size, 10);
                if (!(n > 0) || n > 0x1000000) {
                    return fail('size 不合法（需 1..16777216）', { size: size });
                }
                var bytes = new Uint8Array(p.readByteArray(n));
                var hex = [];
                for (var i = 0; i < bytes.length; i++) {
                    hex.push(('0' + bytes[i].toString(16)).slice(-2));
                }
                return { ok: true, address: p.toString(), size: n, hex: hex.join('') };
            } catch (e) {
                return fail('读取失败：' + e.message, { address: String(address) });
            }
        },

        /**
         * 写内存。
         * 警告：写错地址会立刻崩溃游戏，务必先用 readMemory 确认。
         *
         * 实现要点：**先直接写，只有失败时才改页面保护**。
         * 不要无条件 Memory.protect(..., 'rwx')：那会把本不需要动保护的页面
         * （包括 Frida 自己 Memory.alloc 出来的堆）也改成可执行，
         * 既有安全隐患，也可能让运行时的内存管理出问题（实测会导致脚本
         * 在后续某次 RPC 调用时被销毁）。
         */
        writeMemory: function (address, hex) {
            try {
                var p = ptr(address);
                var s = String(hex).replace(/\s+/g, '');
                if (s.length % 2 !== 0) { return fail('hex 长度必须为偶数'); }
                var bytes = [];
                for (var i = 0; i < s.length; i += 2) {
                    var b = parseInt(s.substr(i, 2), 16);
                    if (isNaN(b)) {
                        return fail('hex 含非法字符：' + s.substr(i, 2));
                    }
                    bytes.push(b);
                }
                if (!bytes.length || bytes.length > 0x100000) {
                    return fail('写入长度不合法（需 1..1048576 字节）');
                }

                var usedProtect = false;
                try {
                    p.writeByteArray(bytes);
                } catch (firstErr) {
                    // 只有当直接写确实失败时，才尝试放宽该页面的保护
                    try {
                        Memory.protect(p, bytes.length, 'rw-');
                        usedProtect = true;
                        p.writeByteArray(bytes);
                    } catch (secondErr) {
                        return fail('写入失败：' + secondErr.message, {
                            address: p.toString(),
                            first_error: firstErr.message
                        });
                    }
                }
                return {
                    ok: true,
                    address: p.toString(),
                    written: bytes.length,
                    relaxed_protection: usedProtect
                };
            } catch (e) {
                return fail('写入失败：' + e.message, { address: String(address) });
            }
        },

        /**
         * AOB 扫描（兜底手段：优先用元数据/RVA 定位，仅在符号缺失时使用）。
         *
         * 注意：本接口**不要求 IL2CPP 初始化成功**。AOB 扫描恰恰是
         * 「符号被剥离 / 非 Unity 游戏 / 找不到 il2cpp 导出」时的兜底手段，
         * 若强制依赖 IL2CPP 就自相矛盾。因此这里只要求能确定一个扫描区间。
         *
         * 区间选择优先级：opts.module 指定的模块 → IL2CPP 模块 → 主可执行模块
         */
        scanPattern: function (opts) {
            opts = opts || {};
            var pattern = String(opts.pattern || '');
            if (!pattern) { return fail('缺少 pattern'); }

            // 尽量解析 IL2CPP 模块，但**允许失败**（非 IL2CPP 目标也要能扫）
            if (!mod) { mod = resolveModule(opts.module); }

            var target = null;
            if (opts.module) {
                target = Process.findModuleByName(opts.module);
                if (!target) { return fail('未找到模块 ' + opts.module); }
            } else if (mod) {
                target = mod;
            } else {
                // 退到主可执行模块：原生游戏/工具也能扫
                var mods = Process.enumerateModules();
                target = mods.length ? mods[0] : null;
                if (!target) {
                    return fail('无法确定扫描区间：既没有 IL2CPP 模块，也没有主模块');
                }
            }

            var maxResults = typeof opts.max_results === 'number' ? opts.max_results : 32;
            var results = [];
            var truncated = false;

            // 只扫落在目标模块内的可读段：更快、误报更少
            var ranges = [];
            try {
                var all = Process.enumerateRanges('r--');
                for (var i = 0; i < all.length; i++) {
                    var r = all[i];
                    if (r.base.compare(target.base) >= 0 &&
                        r.base.compare(target.base.add(target.size)) < 0) {
                        ranges.push(r);
                    }
                }
            } catch (e) {
                ranges = [];
            }
            if (!ranges.length) {
                // 枚举失败时退化为整模块扫描
                ranges = [{ base: target.base, size: target.size }];
            }

            // 注意：**必须用 scanSync**。
            // Memory.scan 是异步的：onMatch 回调在本次 RPC 调用返回之后才执行，
            // 因此用 Memory.scan 收集结果永远是空的（实测确认：同一段内存
            // scanSync 命中 1 处，Memory.scan 命中 0 处）。
            for (var k = 0; k < ranges.length; k++) {
                if (results.length >= maxResults) { truncated = true; break; }
                try {
                    var hits = Memory.scanSync(ranges[k].base, ranges[k].size, pattern);
                    for (var h = 0; h < hits.length; h++) {
                        if (results.length >= maxResults) { truncated = true; break; }
                        var d = describeAddress(hits[h].address);
                        results.push({
                            address: hits[h].address.toString(),
                            size: hits[h].size,
                            rva_hex: d ? d.rva_hex : null
                        });
                    }
                } catch (e) {
                    // 单段不可读：跳过该段而不是整体失败
                    continue;
                }
            }

            return {
                ok: true,
                pattern: pattern,
                scanned_module: target.name,
                scanned_ranges: ranges.length,
                matched: results.length,
                truncated: truncated,
                results: results,
                note: 'AOB 扫描是兜底手段；优先用 classInfo/methodTarget 拿到 RVA 后直接定位'
            };
        },

        /**
         * 任意 JS 求值 —— 应急逃生舱。
         * 与 AiMod 的 eval_script 同定位：当预置工具不够用时，
         * 让 Agent 直接下发 JS 探测。返回值必须是可 JSON 序列化的。
         */
        evalJs: function (code) {
            try {
                // eslint-disable-next-line no-new-func
                var fn = new Function('mod', 'api', 'funcs', 'hooks', 'return (' +
                                      String(code) + ');');
                var v = fn(mod, api, funcs, hooks);
                return { ok: true, value: v === undefined ? null : v };
            } catch (e) {
                // 表达式求值失败时，按语句块再试一次
                try {
                    // eslint-disable-next-line no-new-func
                    var fn2 = new Function('mod', 'api', 'funcs', 'hooks',
                                           String(code));
                    var v2 = fn2(mod, api, funcs, hooks);
                    return { ok: true, value: v2 === undefined ? null : v2 };
                } catch (e2) {
                    return fail('求值失败：' + e2.message, { first_error: e.message });
                }
            }
        },

        /** GC 控制：Hook 期间建议关 GC，避免对象在回调中被回收。 */
        gcControl: function (disable) {
            if (!funcs.il2cpp_gc_disable || !funcs.il2cpp_gc_enable) {
                return fail('缺少 il2cpp_gc_disable / il2cpp_gc_enable 导出');
            }
            try {
                if (disable) { funcs.il2cpp_gc_disable(); gcDisabled = true; }
                else { funcs.il2cpp_gc_enable(); gcDisabled = false; }
                return { ok: true, gc_disabled: gcDisabled };
            } catch (e) {
                return fail('GC 控制失败：' + e.message);
            }
        },

        /**
         * 分配一块**持久**缓冲区。
         *
         * 为什么需要这个：Frida 的 Memory.alloc 返回的内存若在 JS 侧没有引用，
         * 会被脚本 GC 回收（实测：一次 RPC 里 alloc 出的地址，下一次 RPC
         * 去读已经被复用/清零）。这里把指针存进模块级数组，保证在脚本存活
         * 期间稳定有效，供宿主做「写入 → 读回」这类跨调用校验，
         * 也可当作读结构体的临时缓冲区。
         */
        allocBuffer: function (size) {
            var n = parseInt(size, 10);
            if (!(n > 0) || n > 0x1000000) {
                return fail('size 不合法（需 1..16777216）', { size: size });
            }
            try {
                var p = Memory.alloc(n);
                scratch.push({ ptr: p, size: n });
                return { ok: true, address: p.toString(), size: n,
                         note: '该缓冲区在脚本存活期间保持有效' };
            } catch (e) {
                return fail('分配失败：' + e.message);
            }
        },

        /** 释放之前 allocBuffer 出来的缓冲区。 */
        freeBuffer: function (address) {
            var kept = [];
            var removed = 0;
            for (var i = 0; i < scratch.length; i++) {
                if (scratch[i].ptr.toString() === String(address)) {
                    removed++;
                } else {
                    kept.push(scratch[i]);
                }
            }
            scratch = kept;
            return { ok: true, removed: removed };
        },

        /** 列出当前持有的持久缓冲区。 */
        listBuffers: function () {
            return {
                ok: true,
                buffers: scratch.map(function (b) {
                    return { address: b.ptr.toString(), size: b.size };
                })
            };
        }
    };

    // 脚本加载即发送一条就绪消息，便于宿主确认注入成功
    try {
        send({ type: 'probe_ready', frida: Frida.version, pid: Process.id,
               arch: Process.arch });
    } catch (e) {
        // 加载期 send 失败不致命
    }

})();
