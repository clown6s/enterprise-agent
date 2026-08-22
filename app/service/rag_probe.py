# -*- coding: utf-8 -*-
"""RAG 诊断观察器：默认关闭，仅记录脱敏的阶段元数据。"""
import hashlib
import logging
import os

log = logging.getLogger(__name__)


def enabled() -> bool:
    return os.getenv("RAG_DIAGNOSTICS", "0").strip().lower() in {"1", "true", "yes"}


def digest(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


def text_meta(value: str | None) -> dict:
    text = value or ""
    return {"chars": len(text), "hash": digest(text)}


def hit_meta(hit: dict) -> dict:
    return {"document_id": digest(hit.get("document_id", "")),
            "source": digest(hit.get("source", "")),
            "page": hit.get("page", 0), "score": hit.get("score"),
            "expanded": bool(hit.get("_expanded"))}


def emit(stage: str, **fields) -> None:
    if not enabled():
        return
    try:
        log.info("rag_probe", extra={"event": "rag_probe",
                                      "fields": {"stage": stage, **fields}})
    except Exception:
        pass
