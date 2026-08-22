# Upload stage test modules

上传流程拆成独立手动测试脚本，均支持 `upload_test_common.py` 的 `FILE_PATH` 和 `--file` 覆盖。默认只读/打桩，不调用真实 embedding、Milvus、Neo4j，不写生产日志/归档。

阶段脚本：
- test_upload_parse.py：读取/解析
- test_upload_quality.py：质量诊断
- test_upload_structure.py：profile 分类
- test_upload_chunk.py：结构化切分
- test_upload_embedding.py：确定性 mock 向量
- test_upload_milvus.py：捕获待写 rows
- test_upload_graph.py：捕获图谱输入
- test_upload_full_dry_run.py：全链路 dry-run 摘要

FILE_PATH 默认为古代诗歌示例，可直接修改或使用 `--file` 逐个文档验收。