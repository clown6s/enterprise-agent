# -*- coding: utf-8 -*-
"""
摄入管线：解析 → 切分 → 嵌入 → Milvus 入库（sparse_bm25 由服务端自动生成）。

管线 v2：
- 切分：按段落聚合到 max_chars，长段落按句号断；「## 标题」行作为块边界前缀
- 嵌入：批量请求（32 条/批）——曾逐条串行，200 chunk 的文档 200 次 HTTP
- 图谱构建：后台线程——曾同步阻塞上传响应（大文档要等数分钟）
- 去重：内容 sha256 注册表——曾同一文件传两次生成两套 chunk，污染知识库
- 全程落日志 logs/ingestion.jsonl（摄入可观测）
"""
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime

from .. import config, embeddings, milvus_store
from ..service import graph
from . import parse

_LOG = os.path.join(config.LOG_DIR, "ingestion.jsonl")
_INGESTED_FILE = os.path.join(config.LOG_DIR, "ingested.jsonl")   # 内容 sha256 注册表

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


def _embed_batch_with_retry(texts: list[str], retries: int = 2) -> list[list[float]]:
    """批量嵌入重试（整批失败才重试——曾逐条串行，200 chunk=200 次调用）"""
    last: Exception | None = None
    for _ in range(retries):
        try:
            return embeddings.embed_batch(texts)
        except Exception as e:
            last = e
    raise last  # type: ignore[misc]


def _find_duplicate(sha: str) -> dict | None:
    """按内容 sha256 查已入库文档（重复上传防护——曾同一文件传两次生成
    两套 chunk，检索命中重复来源，知识库污染）。
    注册表命中后必须验证文档还在向量库里（Milvus 重建后注册表残留，
    曾把重传同文件当重复跳过——库空着却永远传不进去）；全表扫描，任一
    条记录对应的文档在库即真重复，全残留则清掉并视为新文档。"""
    stale = False
    try:
        with open(_INGESTED_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("sha") != sha:
                    continue
                if milvus_store.has_document(e.get("docId", "")):
                    return e
                stale = True
    except OSError:
        return None
    if stale:
        _prune_stale(sha)
    return None


def _prune_stale(sha: str) -> None:
    """清掉注册表里向量库中已不存在的残留记录（Milvus 重建场景，
    懒清理：只动当前 sha，不做全量 has_document 检查）"""
    try:
        with open(_INGESTED_FILE, encoding="utf-8") as f:
            entries = [json.loads(l) for l in f if l.strip()]
        kept = [e for e in entries
                if not (e.get("sha") == sha
                        and not milvus_store.has_document(e.get("docId", "")))]
        if len(kept) != len(entries):
            with open(_INGESTED_FILE, "w", encoding="utf-8") as f:
                for e in kept:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except (OSError, ValueError):
        pass


def _record_ingested(sha: str, doc_id: str, filename: str, title: str, chunks: int) -> None:
    try:
        os.makedirs(os.path.dirname(_INGESTED_FILE), exist_ok=True)
        with open(_INGESTED_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(), "sha": sha,
                                "docId": doc_id, "file": filename, "title": title,
                                "chunks": chunks}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _graph_build_bg(chunks: list[str], doc_id: str, filename: str) -> None:
    """图谱构建（后台线程）：失败只记日志——软依赖不变，只是不再阻塞上传响应"""
    try:
        rels = graph.build_from_chunks(chunks, doc_id)
        _log("graph_built", file=filename, doc_id=doc_id, graph_rels=rels)
    except Exception as e:
        _log("graph_build_failed", file=filename, doc_id=doc_id, error=str(e)[:200])


def _log(action: str, **fields) -> None:
    entry = {"ts": datetime.now().isoformat(), "action": action, **fields}
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ingest(filename: str, data: bytes, title: str = "", tags: str = "") -> dict:
    """完整摄入：返回 {documentId, fileName, chunks}；失败返回 {error}。
    内容相同文件重复上传返回已有 docId（duplicate=True）——不二次入库。"""
    sha = hashlib.sha256(data).hexdigest()
    dup = _find_duplicate(sha)
    if dup:
        _log("duplicate_skipped", file=filename, sha=sha[:16], doc_id=dup.get("docId"))
        return {"documentId": dup.get("docId", ""), "fileName": filename,
                "chunks": dup.get("chunks", 0), "duplicate": True}
    text = parse.parse_bytes(filename, data)
    if not text or not text.strip():
        _log("parse_failed", file=filename, size=len(data))
        return {"error": f"未从《{filename}》中解析出文本（图片无法识别/不支持的格式）"}
    chunks, pages = chunk_text(text, max_chars=MAX_CHARS, overlap=OVERLAP)
    if not chunks:
        return {"error": f"《{filename}》切分后无内容。"}
    doc_id = uuid.uuid4().hex
    doc_title = (title or filename).strip() or "未命名文档"
    try:
        vecs = _embed_batch_with_retry(chunks)
        rows = [{
            "text": c,
            "dense": vecs[i],
            "document_id": doc_id,
            "title": doc_title,
            "source": doc_title,
            "page": pages[i],
        } for i, c in enumerate(chunks)]
    except Exception as e:
        _log("embed_failed", file=filename, error=str(e)[:200])
        return {"error": f"《{filename}》向量化失败（嵌入服务异常），请稍后重试。"}
    ok = milvus_store.insert_documents(rows)
    # 图谱构建转后台线程（曾同步阻塞上传响应——大文档图谱提取多次 LLM 调用，
    # 上传要等数分钟；软依赖不变：失败只记日志）
    if ok:
        threading.Thread(target=_graph_build_bg, args=(chunks, doc_id, filename),
                         daemon=True).start()
    _log("ingested", file=filename, doc_id=doc_id, chunks=len(chunks), ok=ok)
    if not ok:
        return {"error": f"《{filename}》向量库写入失败。"}
    _record_ingested(sha, doc_id, filename, doc_title, len(chunks))
    return {"documentId": doc_id, "fileName": filename, "chunks": len(chunks)}
