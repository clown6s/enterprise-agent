# -*- coding: utf-8 -*-
"""
摄入管线：解析 → 切分 → 嵌入 → Milvus 入库（sparse_bm25 由服务端自动生成）。

管线 v1：
- 切分：按段落聚合到 max_chars，长段落按句号断；docx 的「## 标题」行作为块边界前缀
- 全程落日志 logs/ingestion.jsonl（摄入可观测）
"""
import json
import os
import re
import uuid
from datetime import datetime

from .. import config, embeddings, milvus_store
from ..service import graph
from . import parse

_LOG = os.path.join(config.LOG_DIR, "ingestion.jsonl")

MAX_CHARS = 600       # 单 chunk 上限（约 300 中文字，BGE-M3 舒适区间）
LONG_CUT = 600        # 长段落按句号切割的参考长度


def chunk_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """段落感知切分：短段落聚合、长段落按句号断"""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 1 <= max_chars:
            buf = f"{buf}\n{p}" if buf else p
            continue
        if buf:
            chunks.append(buf)
        while len(p) > max_chars:
            cut = p.rfind("。", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            chunks.append(p[: cut + 1])
            p = p[cut + 1:]
        buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _log(action: str, **fields) -> None:
    entry = {"ts": datetime.now().isoformat(), "action": action, **fields}
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ingest(filename: str, data: bytes, title: str = "", tags: str = "") -> dict:
    """完整摄入：返回 {documentId, fileName, chunks}；失败返回 {error}"""
    text = parse.parse_bytes(filename, data)
    if not text or not text.strip():
        _log("parse_failed", file=filename, size=len(data))
        return {"error": f"未从《{filename}》中解析出文本（扫描件 PDF/不支持的格式？）"}
    chunks = chunk_text(text)
    if not chunks:
        return {"error": f"《{filename}》切分后无内容。"}
    doc_id = uuid.uuid4().hex
    doc_title = (title or filename).strip() or "未命名文档"
    rows = [{
        "text": c,
        "dense": embeddings.embed(c),
        "document_id": doc_id,
        "title": doc_title,
        "source": doc_title,
    } for c in chunks]
    ok = milvus_store.insert_documents(rows)
    # 图谱构建（软依赖：失败/超量只记日志，不阻断摄入）
    rels = graph.build_from_chunks(chunks, doc_id) if ok else 0
    _log("ingested", file=filename, doc_id=doc_id, chunks=len(chunks), ok=ok,
         graph_rels=rels)
    if not ok:
        return {"error": f"《{filename}》向量库写入失败。"}
    return {"documentId": doc_id, "fileName": filename, "chunks": len(chunks)}
