# -*- coding: utf-8 -*-
"""真实三路检索诊断：dense/BM25/hybrid RRF。"""
from query_test_common import get_args, write_result
from app import embeddings, milvus_store
from app.service import agent


def main():
    args = get_args(); query = args.query
    try:
        vector = embeddings.embed(query)
        result = milvus_store.search_diagnostics(vector, query)
        result.update({"stage": "retrieval_diagnostics", "query": query,
                       "extra_queries": agent._expand_queries(query)})
    except Exception as exc:
        result = {"stage": "retrieval_diagnostics", "query": query,
                  "status": "error", "error_type": type(exc).__name__,
                  "error": str(exc)[:500]}
    write_result(query, "retrieval", result, args.output)


if __name__ == "__main__":
    main()
