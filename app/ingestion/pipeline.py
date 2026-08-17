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

MAX_CHARS = 600       # 单 chunk 上限
OVERLAP = 90          # 断点重叠（15%），语义不割裂

_PAGE_MARK = re.compile(r"^\[\[page:(\d+)\]\]$")


def chunk_text(text: str, max_chars: int = MAX_CHARS,
               overlap: int = 90) -> tuple[list[str], list[int]]:
    """
    段落感知切分：短段落聚合、长段落按句号断，断点带 15% 重叠（90 字，
    语义不割裂）。「## 标题」行作为块边界：先 flush 当前块，标题作为下一块
    的开头。[[page:N]] 标记逐页追踪页码——返回 (chunks, pages)。
    """
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    pages: list[int] = []
    buf = ""
    buf_page = 1

    def flush():
        nonlocal buf
        if buf:
            chunks.append(buf)
            pages.append(buf_page)
            buf = ""

    for p in paragraphs:
        m = _PAGE_MARK.match(p)
        if m:
            flush()
            buf_page = int(m.group(1))
            continue
        if p.startswith("## "):
            flush()
            buf = p            # 标题成为新块的开头
            continue
        if len(buf) + len(p) + 1 <= max_chars:
            buf = f"{buf}\n{p}" if buf else p
            continue
        flush()
        while len(p) > max_chars:
            cut = p.rfind("。", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            piece = p[: cut + 1]
            chunks.append(piece)
            pages.append(buf_page)
            # 重叠续接：下一段从上一块尾部带 overlap 字开始，断点语义不割裂
            tail = piece[-overlap:] if overlap and len(piece) > overlap else ""
            p = (tail + p[cut + 1:]).strip()
        buf = p
    flush()
    return chunks, pages


def _embed_with_retry(text: str, retries: int = 2) -> list[float]:
    """嵌入重试：嵌入服务瞬时抖动不整单失败（曾无兜底直接 500）"""
    last: Exception | None = None
    for _ in range(retries):
        try:
            return embeddings.embed(text)
        except Exception as e:
            last = e
    raise last  # type: ignore[misc]


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
    chunks, pages = chunk_text(text, max_chars=MAX_CHARS, overlap=OVERLAP)
    if not chunks:
        return {"error": f"《{filename}》切分后无内容。"}
    doc_id = uuid.uuid4().hex
    doc_title = (title or filename).strip() or "未命名文档"
    try:
        rows = [{
            "text": c,
            "dense": _embed_with_retry(c),
            "document_id": doc_id,
            "title": doc_title,
            "source": doc_title,
            "page": pages[i],
        } for i, c in enumerate(chunks)]
    except Exception as e:
        _log("embed_failed", file=filename, error=str(e)[:200])
        return {"error": f"《{filename}》向量化失败（嵌入服务异常），请稍后重试。"}
    ok = milvus_store.insert_documents(rows)
    # 图谱构建（软依赖：失败/超量只记日志，不阻断摄入）
    rels = graph.build_from_chunks(chunks, doc_id) if ok else 0
    _log("ingested", file=filename, doc_id=doc_id, chunks=len(chunks), ok=ok,
         graph_rels=rels)
    if not ok:
        return {"error": f"《{filename}》向量库写入失败。"}
    return {"documentId": doc_id, "fileName": filename, "chunks": len(chunks)}
