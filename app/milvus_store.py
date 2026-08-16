# -*- coding: utf-8 -*-
"""
Milvus 向量库访问（pymilvus ORM 接口）。

集合 schema（与 Java 版同一套）：
    id(PK) / text / sparse_bm25 / dense(1024维) / document_id / title / source

search() 目前只用稠密向量（dense）。BM25 稀疏召回是路线图（混合检索），
Java 版也是后补的——先保证主链路可用。
"""
from pymilvus import Collection, connections

from . import config

_collection = None


def _col() -> Collection:
    """懒连接单例：进程启动后第一次检索时才连 Milvus（软依赖）"""
    global _collection
    if _collection is None:
        connections.connect(
            host=config.MILVUS_HOST, port=config.MILVUS_PORT, db_name=config.MILVUS_DB
        )
        _collection = Collection(config.MILVUS_COLLECTION)
        _collection.load()
    return _collection


def search(vector: list[float], top_k: int = config.TOP_K) -> list[dict]:
    """
    稠密向量相似检索。返回 [{text, document_id, title, source, score}] 按分数降序。
    Milvus 不可用时返回空列表（软依赖：问答降级为兜底句式，不崩服务）。
    """
    try:
        hits = _col().search(
            data=[vector],
            anns_field="dense",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=top_k,
            output_fields=["text", "document_id", "title", "source"],
        )
        return [
            {
                "text": h.entity.get("text", ""),
                "document_id": h.entity.get("document_id", ""),
                "title": h.entity.get("title", ""),
                "source": h.entity.get("source", ""),
                "score": float(h.distance),
            }
            for h in hits[0]
        ]
    except Exception:
        return []
