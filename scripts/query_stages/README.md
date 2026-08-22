# 查询阶段测试

本目录测试查询链路的每个阶段。默认真实调用 embedding、Milvus、rerank、生成模型和校验模型，结果只写 `outputs/query_stages/`，不写历史、反馈、调参、Milvus 或 Neo4j。

公共配置在 `query_test_common.py`：

```python
QUERY = "高血压一日三餐应该注意什么"
```

也可使用 `--query` 覆盖。

阶段顺序：

```text
retrieval → rerank → context → generate → verify → full
```

执行示例：

```bash
PYTHONUTF8=1 python scripts/query_stages/test_query_retrieval.py --query "高血压应该注意什么"
PYTHONUTF8=1 python scripts/query_stages/test_query_rerank.py --query "高血压应该注意什么"
PYTHONUTF8=1 python scripts/query_stages/test_query_context.py --query "高血压应该注意什么"
PYTHONUTF8=1 python scripts/query_stages/test_query_generate.py --query "高血压应该注意什么"
PYTHONUTF8=1 python scripts/query_stages/test_query_verify.py --query "高血压应该注意什么"
PYTHONUTF8=1 python scripts/query_stages/test_query_full.py --query "高血压应该注意什么"
```

输出文件位于：

```text
outputs/query_stages/
```

`retrieval` 中的 `score` 是 Milvus 混合检索的 RRF 融合分数；当前接口没有暴露 dense/BM25 分支分数，因此不会伪造这两个分数。为空集合时输出 error/status，不继续假装 rerank 成功。