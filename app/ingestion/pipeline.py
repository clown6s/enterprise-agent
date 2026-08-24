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
from ..time_context import now as business_now
from . import parse

_LOG = os.path.join(config.LOG_DIR, "ingestion.jsonl")
_INGESTED_FILE = os.path.join(config.LOG_DIR, "ingested.jsonl")   # 内容 sha256 注册表
# 原始文档留底目录（根备份：向量/图谱都可重建，原始文件不可——曾 Milvus
# 重建后只能靠用户重传，本地文件再丢就彻底归零）
_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "uploads")

MAX_CHARS = 600       # 单 chunk 上限
OVERLAP = 90          # 断点重叠（15%），语义不割裂

_PAGE_MARK = re.compile(r"^\[\[page:(\d+)\]\]$")


def chunk_units(text: str, filename: str = "", max_chars: int = MAX_CHARS) -> list[dict]:
    """切分为带完整来源元数据的 unit 字典。

    这是摄入管线的内部规范接口；``chunk_text`` 仅保留旧调用方需要的
    ``(chunks, pages)`` 形状，避免旧 HTTP/脚本协议被悄然改变。
    """
    from .structured_chunking import chunk_document, classify_document, segment_text
    profile = classify_document(text, filename)
    if profile.kind == 'technical_book':
        return chunk_document(text, filename, max_chars=max_chars)
    # 普通文本继续走通用切分，避免 profile 误判时专用切分器丢内容。
    return segment_text(text, max_chars=max_chars, overlap=0)


def chunk_text(text: str, max_chars: int = MAX_CHARS,
               overlap: int = 90) -> tuple[list[str], list[int]]:
    """兼容旧接口；overlap 保留但由结构化切分器统一决定。"""
    units = chunk_units(text, max_chars=max_chars)
    return [u['text'] for u in units], [u.get('page', 1) for u in units]


def _build_vector_rows(units: list[dict], vectors: list[list[float]],
                       doc_id: str, doc_title: str) -> list[dict]:
    """Build Milvus rows while carrying chunk-unit metadata through unchanged."""
    metadata_keys = ('kind', 'page_end', 'continuation', 'continuation_of',
                     'continuation_index', 'section_path')
    rows = []
    for i, unit in enumerate(units):
        row = {
            'text': unit.get('text', ''), 'dense': vectors[i],
            'document_id': doc_id, 'title': doc_title, 'source': doc_title,
            'page': unit.get('page', 1),
        }
        for key in metadata_keys:
            if key in unit:
                row[key] = unit[key]
        rows.append(row)
    return rows


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
            f.write(json.dumps({"ts": business_now().isoformat(), "sha": sha,
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
    entry = {"ts": business_now().isoformat(), "action": action, **fields}
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
        # 重复上传也补留底（留底机制上线前的历史文档，重传一次即补齐根备份）
        _archive_original(sha, filename, data)
        return {"documentId": dup.get("docId", ""), "fileName": filename,
                "chunks": dup.get("chunks", 0), "duplicate": True}
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    diagnostics = None
    if ext == "pdf":
        text, diagnostics = parse.parse_pdf_with_diagnostics(data)
        _log("pdf_quality_gate", file=filename, quality_action=diagnostics.get("action"),
             reasons=";".join(diagnostics.get("reasons") or [])[:200],
             usable_chars=diagnostics.get("usable_chars", 0),
             terminal_fragments=diagnostics.get("terminal_fragment_count", 0))
        if diagnostics.get("action") == "reject":
            reason = "；".join(diagnostics.get("reasons") or []) or "PDF 质量检查未通过"
            _log("pdf_quality_gate", file=filename, action="reject",
                 reasons=reason[:200], usable_chars=diagnostics.get("usable_chars", 0))
            return {"error": f"《{filename}》未通过 PDF 质量检查：{reason}"}
    else:
        text = parse.parse_bytes(filename, data)
    if not text or not text.strip():
        reason = parse.explain_parse_failure(filename, data)
        _log("parse_failed", file=filename, size=len(data), reason=reason)
        return {"error": f"未从《{filename}》中解析出文本：{reason}"}
    units = chunk_units(text, filename=filename, max_chars=MAX_CHARS)
    if not units:
        return {"error": f"《{filename}》切分后无内容。"}
    doc_id = uuid.uuid4().hex
    doc_title = (title or filename).strip() or "未命名文档"
    try:
        vecs = _embed_batch_with_retry([u['text'] for u in units])
        rows = _build_vector_rows(units, vecs, doc_id, doc_title)
    except Exception as e:
        _log("embed_failed", file=filename, error=str(e)[:200])
        return {"error": f"《{filename}》向量化失败（嵌入服务异常），请稍后重试。"}
    ok = milvus_store.insert_documents(rows)
    # 图谱构建转后台线程（曾同步阻塞上传响应——大文档图谱提取多次 LLM 调用，
    # 上传要等数分钟；软依赖不变：失败只记日志）
    if ok:
        threading.Thread(target=_graph_build_bg,
                         args=([u['text'] for u in units], doc_id, filename),
                         daemon=True).start()
    _log("ingested", file=filename, doc_id=doc_id, chunks=len(units), ok=ok)
    if not ok:
        return {"error": f"《{filename}》向量库写入失败。"}
    _record_ingested(sha, doc_id, filename, doc_title, len(units))
    _archive_original(sha, filename, data)
    result = {"documentId": doc_id, "fileName": filename, "chunks": len(units)}
    if diagnostics is not None:
        result["diagnostics"] = diagnostics
        if diagnostics.get("reasons"):
            result["warnings"] = diagnostics["reasons"]
    return result


def _archive_original(sha: str, filename: str, data: bytes) -> None:
    """原始文档留底（sha256 命名 + 扩展名）——根备份，软依赖"""
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else "bin"
    if not re.match(r"^[a-z0-9]{1,8}$", ext):
        ext = "bin"
    try:
        os.makedirs(_UPLOADS_DIR, exist_ok=True)
        with open(os.path.join(_UPLOADS_DIR, f"{sha}.{ext}"), "wb") as f:
            f.write(data)
    except OSError:
        pass
