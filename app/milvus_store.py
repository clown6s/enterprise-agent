# -*- coding: utf-8 -*-
"""
Milvus 向量库访问（pymilvus MilvusClient API——ORM 接口 3.1 将移除，
本文件已全量迁移，不再 import Collection/utility/connections）。

集合 schema（documents_v2）：
    id(PK) / text(中文分词) / sparse_bm25(BM25 函数输出) / dense(4096维) /
    document_id / title / source / page(溯源页码)

search() 用混合检索：稠密向量（语义召回）+ BM25（关键词召回）RRF 融合。
纯稠密检索的历史教训：关键词类问法（"今年国庆放几天"）语义向量召不回
（纯稠密 62.5%，加 BM25 混合后关键词面才补上），BM25 补的就是关键词面。

create_collection_v2() 建集合（含 BM25 函数字段与 HNSW/稀疏索引）；
旧集合（documents）仅作迁移源，见 scripts/migrate_v2.py。
"""
import re
import threading
import time

from pymilvus import (AnnSearchRequest, CollectionSchema, DataType, FieldSchema,
                      Function, FunctionType, MilvusClient, RRFRanker)
from pymilvus.milvus_client import IndexParams

from . import config

_client = None
_last_ping: dict = {"ts": 0.0}
_PING_TTL = 30   # 连接心跳间隔（秒）
_lock = threading.Lock()


def _uri() -> str:
    return f"http://{config.MILVUS_HOST}:{config.MILVUS_PORT}"


def _ensure_database() -> None:
    """业务库幂等就绪（裸 Milvus 首启自动建库——上传路径不再依赖人工建库；
    曾只靠迁移脚本建集合，Milvus 重建后首次上传必失败）。
    db 管理用临时 client（default 库）→ close；业务 client 构造时直接带 db_name，
    纯 MilvusClient 下无 ORM 注册表占位连接打架问题（曾 ConnectionNotExist）。"""
    try:
        client = MilvusClient(uri=_uri())
        try:
            if config.MILVUS_DB not in client.list_databases():
                client.create_database(config.MILVUS_DB)
                print(f"[milvus] 数据库 {config.MILVUS_DB} 已创建")
        finally:
            client.close()
    except Exception as e:
        print(f"[milvus] 数据库就绪检查失败（继续尝试直连业务库）: {e!r}")


def _build_schema() -> CollectionSchema:
    return CollectionSchema(
        fields=[
            FieldSchema("id", DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema("text", DataType.VARCHAR, max_length=6000,
                        enable_analyzer=True,
                        analyzer_params={"type": "chinese"}),
            FieldSchema("sparse_bm25", DataType.SPARSE_FLOAT_VECTOR),
            FieldSchema("dense", DataType.FLOAT_VECTOR, dim=config.EMBED_DIM),
            FieldSchema("document_id", DataType.VARCHAR, max_length=128),
            FieldSchema("title", DataType.VARCHAR, max_length=512),
            FieldSchema("source", DataType.VARCHAR, max_length=512),
            FieldSchema("page", DataType.INT64, default_value=1),
        ],
        functions=[Function(
            name="text_bm25", function_type=FunctionType.BM25,
            input_field_names=["text"], output_field_names=["sparse_bm25"])],
    )


def _build_index_params() -> IndexParams:
    ip = IndexParams()
    ip.add_index(field_name="dense", index_name="dense",
                 index_type="HNSW", metric_type="COSINE",
                 params={"M": 16, "efConstruction": 200})
    ip.add_index(field_name="sparse_bm25", index_name="sparse_bm25",
                 index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    # document_id 标量倒排索引：delete/rename/ACL 过滤按 document_id 走索引
    # 而非全表扫描（曾无标量索引——文档量上来后删除/改名/过滤全表扫）
    ip.add_index(field_name="document_id", index_name="document_id",
                 index_type="INVERTED")
    return ip


def _ensure_collection(client: MilvusClient) -> bool:
    """幂等建集合（不存在才建）；索引按名字缺失补齐（含已存在的集合——
    曾只在新集合上建索引，旧集合补不上新加字段的索引。存在性探测用
    list_indexes 名字比对——曾用重复建索引靠异常探测，Milvus 报 1100
    "at most one distinct index per field" 而非 "already exist"）。"""
    name = config.MILVUS_COLLECTION
    try:
        if not client.has_collection(name):
            client.create_collection(name, schema=_build_schema())
            print(f"[milvus] 集合 {name} 已创建（dense={config.EMBED_DIM}维）")
        existing = set(client.list_indexes(name))
        for ip in _build_index_params():
            if ip.index_name in existing:
                continue
            client.create_index(name, IndexParams([ip]))
            print(f"[milvus] 索引 {ip.field_name} 已补建")
        return True
    except Exception as e:
        print(f"[milvus] 集合创建失败: {e!r}")
        return False


def _get_client() -> MilvusClient:
    """懒连接单例：首次调用自动建库建集合再加载；30s TTL 心跳检测
    Milvus 服务端重启后的陈旧连接并自动重连，重连后重新 load（服务端重启
    集合会脱载——曾无重连，服务端重启后应用永远空检索直到自身重启，
    部署事故同款模式）。并发首启用锁+双检，只建一次。"""
    global _client
    now = time.time()
    if _client is not None and now - _last_ping["ts"] > _PING_TTL:
        try:
            _client.has_collection(config.MILVUS_COLLECTION)   # 轻量心跳（走同一通道）
            _last_ping["ts"] = now
        except Exception:
            print("[milvus] 连接失效，自动重连")
            try:
                _client.close()
            except Exception:
                pass
            _client = None
    if _client is None:
        with _lock:
            if _client is None:
                try:
                    _ensure_database()
                except Exception as e:
                    print(f"[milvus] 数据库就绪失败: {e!r}")
                _client = MilvusClient(uri=_uri(), db_name=config.MILVUS_DB,
                                       timeout=30)   # 请求超时：检索/写入不无限挂起
                try:
                    _ensure_collection(_client)
                    # 幂等 load：已加载为 no-op；覆盖服务端重启后集合脱载的场景
                    _client.load_collection(config.MILVUS_COLLECTION)
                except Exception as e:
                    print(f"[milvus] 集合就绪/加载失败: {e!r}")
                _last_ping["ts"] = time.time()
    return _client


def create_collection_v2(drop_existing: bool = False) -> bool:
    """建 documents_v2（migrate_v2 兼容入口；运行时引导走 _get_client()）。
    已存在且不 drop → 直接 True。失败返回 False（软依赖）。"""
    name = config.MILVUS_COLLECTION
    try:
        client = _get_client()
        if drop_existing and client.has_collection(name):
            client.drop_collection(name)
        ok = _ensure_collection(client)
        if ok:
            client.load_collection(name)   # drop 重建后必须重新加载
        return ok
    except Exception as e:
        print(f"[milvus] 集合创建失败: {e!r}")
        return False


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
        hits = _search_with_retry(
            lambda: _get_client().hybrid_search(
                config.MILVUS_COLLECTION,
                reqs=[dense_req, sparse_req],
                ranker=RRFRanker(),
                limit=top_k,
                output_fields=["text", "document_id", "title", "source", "page"],
            ))
        return [
            {
                "text": h.entity.get("text", ""),
                "document_id": h.entity.get("document_id", ""),
                "title": h.entity.get("title", ""),
                "source": h.entity.get("source", ""),
                "page": int(h.entity.get("page", 0) or 0),
                "score": float(h.distance),
            }
            for h in hits[0]
        ]
    except Exception as e:
        # 失败可见：挂了 vs 没命中必须可区分（此前静默返回空，诊断无从下手）
        print(f"[milvus] 混合检索失败: {e!r}")
        return []


def _search_with_retry(fn):
    """检索重试一次（瞬时抖动：segment compaction/索引切换——ragflow ES
    侧有重试、infinity 无重试挂死的对照教训；挂死场景有 30s 心跳重连兜底）"""
    for attempt in (0, 1):
        try:
            return fn()
        except Exception:
            if attempt == 1:
                raise
            time.sleep(0.5)


def insert_document(text: str, vector: list[float], document_id: str,
                    title: str, source: str) -> bool:
    """
    单 chunk 文档入库（反馈采纳回流用）。sparse_bm25 是函数输出字段，
    插入时只给 text，服务端自动算 BM25。
    返回是否成功；失败不抛异常（软依赖）。
    """
    try:
        _get_client().insert(config.MILVUS_COLLECTION, [{
            "text": text,
            "dense": vector,
            "document_id": document_id,
            "title": title,
            "source": source,
            "page": 1,
        }])
        _get_client().flush(config.MILVUS_COLLECTION)
        return True
    except Exception:
        return False


def insert_documents(rows: list[dict]) -> bool:
    """批量 chunk 入库（摄入管线用）；返回是否成功"""
    try:
        _get_client().insert(config.MILVUS_COLLECTION, rows)
        _get_client().flush(config.MILVUS_COLLECTION)
        return True
    except Exception:
        return False


def _iter_rows(client: MilvusClient, expr: str, output_fields: list[str]) -> list[dict]:
    """全量查询（query_iterator 流式拉取——ORM query 的 offset 翻页
    深翻页代价大且 client.query 无 limit/offset 参数）。"""
    rows: list[dict] = []
    it = client.query_iterator(
        config.MILVUS_COLLECTION, batch_size=1000, filter=expr,
        output_fields=output_fields)
    try:
        while True:
            batch = it.next()
            if not batch:
                break
            rows.extend(batch)
    finally:
        try:
            it.close()
        except Exception:
            pass
    return rows


def list_documents() -> list[dict]:
    """文档列表（按 document_id 去重）：[{docId, title, chunks}]"""
    docs: dict[str, dict] = {}
    try:
        for r in _iter_rows(_get_client(), "id >= 0", ["document_id", "title"]):
            doc_id = r.get("document_id", "")
            if not doc_id:
                continue
            if doc_id in docs:
                docs[doc_id]["chunks"] += 1
                continue
            docs[doc_id] = {"docId": doc_id, "title": r.get("title", ""), "chunks": 1}
    except Exception:
        pass
    return list(docs.values())


# document_id 合法字符白名单（expr 注入防线：曾直接 f-string 拼 Milvus
# 表达式——doc_id 含引号可破坏 expr 删掉别的文档）
_SAFE_DOC_ID = re.compile(r"^[0-9a-zA-Z\-_]{1,128}$")


def has_document(document_id: str) -> bool:
    """按 document_id 查是否存在（OCR 入库去重用——曾同一识别内容重复
    入库产生重复 chunk）"""
    if not _SAFE_DOC_ID.match(document_id or ""):
        return False
    try:
        return bool(_iter_rows(_get_client(), f'document_id == "{document_id}"', ["id"]))
    except Exception:
        return False


def update_document_meta(document_id: str, title: str | None = None,
                         source: str | None = None) -> int:
    """按 document_id 更新全部 chunk 的标题/来源（知识库文档改名治理用）。
    返回更新条数；失败返回 0。"""
    if not title and not source:
        return 0
    if not _SAFE_DOC_ID.match(document_id or ""):
        return 0
    try:
        rows = _iter_rows(_get_client(), f'document_id == "{document_id}"', ["*"])
        if not rows:
            return 0
        for r in rows:
            if title:
                r["title"] = title
            if source:
                r["source"] = source
        _get_client().upsert(config.MILVUS_COLLECTION, rows)
        _get_client().flush(config.MILVUS_COLLECTION)
        return len(rows)
    except Exception as e:
        print(f"[milvus] 元数据更新失败: {e!r}")
        return 0


def delete_by_document_id(document_id: str) -> int:
    """按 document_id 删除全部 chunk（知识库删文档用）"""
    if not _SAFE_DOC_ID.match(document_id or ""):
        return 0
    try:
        res = _get_client().delete(config.MILVUS_COLLECTION,
                                   filter=f'document_id == "{document_id}"')
        return int(res.get("delete_count", 0))
    except Exception:
        return 0


def dense_search(vector: list[float], top_k: int, expr: str | None = None) -> list[dict]:
    """纯稠密检索（混合检索异常时的回退）；expr：文档级过滤（ACL 权限）"""
    try:
        hits = _search_with_retry(
            lambda: _get_client().search(
                config.MILVUS_COLLECTION,
                data=[vector],
                anns_field="dense",
                search_params={"metric_type": "COSINE", "params": {"nprobe": 16}},
                limit=top_k,
                output_fields=["text", "document_id", "title", "source", "page"],
                filter=expr,
            ))
        return [
            {
                "text": h.entity.get("text", ""),
                "document_id": h.entity.get("document_id", ""),
                "title": h.entity.get("title", ""),
                "source": h.entity.get("source", ""),
                "page": int(h.entity.get("page", 0) or 0),
                "score": float(h.distance),
            }
            for h in hits[0]
        ]
    except Exception as e:
        print(f"[milvus] 纯稠密检索失败: {e!r}")
        return []
