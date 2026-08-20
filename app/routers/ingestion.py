# -*- coding: utf-8 -*-
"""文档上传（知识库管理页对接；无鉴权——单机场景）"""
import logging

from fastapi import APIRouter, File, UploadFile

from ..ingestion import pipeline

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50MB 上限（曾无限制，超大文件会整块读入内存）


@router.post("/api/rag/documents/upload")
def upload(file: UploadFile = File(...), title: str = "", tags: str = ""):
    # sync def：FastAPI 自动放线程池执行——曾 async def 里同步跑解析/OCR/嵌入，
    # 上传大文档期间整个事件循环被阻塞，8080 所有 API（含 SSE 问答）卡死
    data = file.file.read()
    log.info("upload %s size=%d", file.filename or "未命名", len(data))
    if len(data) > _MAX_UPLOAD_BYTES:
        log.warning("upload 超限拒绝 %s size=%d", file.filename or "未命名", len(data))
        return {"success": False,
                "message": f"文件超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限，请拆分后上传。",
                "data": None}
    result = pipeline.ingest(file.filename or "未命名", data, title, tags)
    if "error" in result:
        log.warning("upload 失败 %s: %s", file.filename or "未命名", result["error"])
        return {"success": False, "message": result["error"], "data": None}
    log.info("upload 成功 %s docId=%s chunks=%s duplicate=%s",
             file.filename or "未命名", result["documentId"],
             result.get("chunks"), result.get("duplicate", False))
    return {"success": True, "message": None,
            "data": {"documentId": result["documentId"],
                     "fileName": result["fileName"],
                     "fileSize": len(data),
                     "duplicate": result.get("duplicate", False)}}
