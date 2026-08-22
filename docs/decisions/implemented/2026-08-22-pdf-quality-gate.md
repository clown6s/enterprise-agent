# Problem

PDF 质量诊断报告此前没有接入摄入管线，导致存在乱码、页脚污染和残句的 PDF 仍被标记为成功并写入 Milvus。`ingest()` 只能表达写入成功，不能表达内容质量风险。

# Decision

PDF 在 duplicate 快速路径之后调用 `parse_pdf_with_diagnostics()` 一次。报告为 `reject` 时在切分、嵌入和 Milvus 写入前返回现有 error 结构；`allow/warn/ocr` 继续摄入，并在成功结果中增量返回 `diagnostics`/`warnings`。非 PDF 和重复上传保持原有契约。Web 上传成功 data 透传新增字段，旧字段不变；飞书忽略额外字段。质量拒绝记录安全的 `pdf_quality_gate` 事件，不记录正文。

# Consequences

严重质量文档不会继续污染向量库；中度风险文档仍可用但上传结果会暴露 warning。已有旧 rows 不会自动修复，需删除后重新上传。当前质量报告算法仍需用真实文档校准，不能将任何 warning 自动视为 reject。
