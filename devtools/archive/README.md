# archive/ —— 归档的过程产物（**不是验收的一部分**）

这里的脚本是**一次性侦察脚本**：当初为了回答一个具体问题临时写的，
用来反推 IL2CPP v31 的记录布局、找出字段类型解析规则
（「比 `typeIndex` 小的最大 byval 键」）、确认 pak 索引头的字段顺序等等。

**为什么不删**：它们逐字节地打印原始结构。真有一天某个版本的游戏格式变了、
索引解析出错，重新定位的第一步就是把它们翻出来跑。
`verify_*.py` 回答的是「现在的实现对不对」，这里回答的是「这个格式到底长什么样」。

**为什么不放回 `devtools/` 根目录**：`devtools/` 根下的 `verify_*.py` 是**门禁**，
红一条就不许提交；把 31 个「结论已经沉淀进文档、脚本本身不再维护」的文件
和门禁混在一起，会让「`devtools/` 里都是要跑的东西」这个判断失效。
归档目录的语义很清楚：**要跑门禁请跑 `devtools/run_all_verify.py`，
这里的东西不参与。**

## 使用前请注意（它们**不保证**还能跑）

- 脚本里的样本路径是按当时的工作区写的，**可能已经失效**；
  现在样本路径的唯一真值是 `devtools/samples.py`。
- 一些脚本调用的内部 API 可能已经改名或删除。
- 它们**从不**被 `run_all_verify.py` 收集（文件名不以 `verify_` 开头），
  也没有人保证它们仍然输出正确结论 —— 归档 = 不再维护。

要重新用它定位问题时，把结论写回 `docs/` 或转成一个 `verify_*.py` 断言，
不要让它散着留在归档里当第二份真相。

## 内容（31 个：30 个 `.py` + 1 个 `.ps1`，计数是跑出来的）

| 前缀 | 个数 | 方向 |
|---|---|---|
| `probe_field*.py` | 9 | IL2CPP 字段类型/偏移的解析规则 |
| `probe_residual*.py` | 4 | 解析残留（未被任何轨道覆盖的记录） |
| `probe_typedef*.py` | 2 | `typedef` 索引语义 |
| `probe_method*.py` | 2 | 方法表与参数计数（IL2CPP 与 Mono 对照） |
| `probe_final_methods.py` | 1 | 方法表最终结论的复核 |
| `probe_il2cpptype.py` / `probe_typeresolve.py` / `probe_indexspace.py` | 3 | 类型索引空间 |
| `probe_crossgame.py` / `probe_mismatch.py` | 2 | 跨游戏对照与差异定位 |
| `probe_nested.py` / `probe_parent_resolve.py` / `probe_hierarchy.py` / `probe_declaring.py` | 4 | 嵌套类型与父类解析 |
| `probe_metadata.py` / `probe_mono_paramcount.py` / `probe_tiling.py` | 3 | 元数据总览与其它 |
| `_recon_pe.ps1` | 1 | PE 结构侦察（PowerShell） |
