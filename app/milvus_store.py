# -*- coding: utf-8 -*-
"""
Milvus 向量库访问（pymilvus ORM 接口）。

集合 schema：
    id(PK) / text(中文分词) / sparse_bm25(BM25 函数输出) / dense(1024维) / document_id / title / source

search() 用混合检索：稠密向量（语义召回）+ BM25（关键词召回）RRF 融合。
纯稠密检索的历史教训：关键词类问法（"今年国庆放几天"）语义向量召不回
（纯稠密 62.5%，加 BM25 混合后关键词面才补上），BM25 补的就是关键词面。
"""
from pymilvus import AnnSearchRequest, Collection, RRFRanker, connections

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


def search(vector: list[float], query_text: str, top_k: int = config.TOP_K,
           expr: str | None = None) -> list[dict]:
    """
    混合检索（dense + BM25，RRF 融合）。返回 [{text, document_id, title, source, score}]
    按融合分降序。expr：文档级过滤（ACL 权限，None=不过滤）。
    Milvus 不可用返回空列表（软依赖：问答降级为兜底句式，不崩服务）。
    """
    try:
        dense_req = AnnSearchRequest(
            data=[vector],
            anns_field="dense",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=top_k,
            expr=expr,
        )
        # BM25 函数字段：搜索数据直接传原文，服务端用 text_bm25 函数分词打分
        sparse_req = AnnSearchRequest(
            data=[query_text],
            anns_field="sparse_bm25",
            param={"metric_type": "BM25"},
            limit=top_k,
            expr=expr,
        )
        hits = _col().hybrid_search(
            [dense_req, sparse_req],
            rerank=RRFRanker(),
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
    except Exception as e:
        # 失败可见：挂了 vs 没命中必须可区分（此前静默返回空，诊断无从下手）
        print(f"[milvus] 混合检索失败: {e!r}")
        return []


def insert_document(text: str, vector: list[float], document_id: str,
                    title: str, source: str) -> bool:
    """
    单 chunk 文档入库（反馈采纳回流用）。sparse_bm25 是函数输出字段，
    插入时只给 text，服务端自动算 BM25。
    返回是否成功；失败不抛异常（软依赖）。
    """
    try:
        _col().insert([{
            "text": text,
            "dense": vector,
            "document_id": document_id,
            "title": title,
            "source": source,
        }])
        _col().flush()
        return True
    except Exception:
        return False


def insert_documents(rows: list[dict]) -> bool:
    """批量 chunk 入库（摄入管线用）；返回是否成功"""
    try:
        _col().insert(rows)
        _col().flush()
        return True
    except Exception:
        return False


def list_documents() -> list[dict]:
    """文档列表（按 document_id 去重）：[{docId, title, chunks}]"""
    docs: dict[str, dict] = {}
    try:
        offset = 0
        while offset < 100_000:
            res = _col().query(
                expr="id >= 0", output_fields=["document_id", "title"],
                offset=offset, limit=1000,
            )
            if not res:
                break
            for r in res:
                doc_id = r.get("document_id", "")
                if not doc_id or doc_id in docs:
                    docs[doc_id]["chunks"] += 1
                    continue
                docs[doc_id] = {"docId": doc_id, "title": r.get("title", ""), "chunks": 1}
            if len(res) < 1000:
                break
            offset += 1000
    except Exception:
        pass
    return list(docs.values())


def update_document_meta(document_id: str, title: str | None = None,
                         source: str | None = None) -> int:
    """按 document_id 更新全部 chunk 的标题/来源（知识库文档改名治理用）。
    返回更新条数；失败返回 0。"""
    if not title and not source:
        return 0
    try:
        rows = _col().query(expr=f'document_id == "{document_id}"',
                            output_fields=["*"], limit=1000)
        if not rows:
            return 0
        for r in rows:
            if title:
                r["title"] = title
            if source:
                r["source"] = source
        _col().upsert(rows)
        _col().flush()
        return len(rows)
    except Exception as e:
        print(f"[milvus] 元数据更新失败: {e!r}")
        return 0


def delete_by_document_id(document_id: str) -> int:
    """按 document_id 删除全部 chunk（知识库删文档用）"""
    try:
        res = _col().delete(expr=f'document_id == "{document_id}"')
        return int(getattr(res, "delete_count", 0))
    except Exception:
        return 0


def dense_search(vector: list[float], top_k: int, expr: str | None = None) -> list[dict]:
    """纯稠密检索（混合检索异常时的回退）；expr：文档级过滤（ACL 权限）"""
    try:
        hits = _col().search(
            data=[vector],
            anns_field="dense",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=top_k,
            output_fields=["text", "document_id", "title", "source"],
            expr=expr,
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
    except Exception as e:
        print(f"[milvus] 纯稠密检索失败: {e!r}")
        return []
