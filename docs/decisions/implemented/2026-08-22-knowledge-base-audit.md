# Problem

知识库和图谱经过多次重新摄入、删除和重建，不能只依赖上传成功或单条问答判断质量。需要一次只读全量审计，逐行检查 Milvus 文本和图谱归属，不改变任何数据。

# Decision

新增 `scripts/audit_knowledge_base.py`，扫描 `documents_v2` 的标量字段并执行 Neo4j 只读查询。检查空/过短 chunk、替换/私用区字符、页标记残留、终端残片、元数据缺失、重复 chunk、图谱无 doc_id/孤立 doc_id/自环，以及日志记录数量。报告只输出计数、hash 和有限样例，不输出完整正文；结果保存到 `logs/knowledge-audit.json`。

脚本不调用写操作、备份、恢复或重建脚本。审计结果先用于定位问题，再由用户决定是否修复。
