# -*- coding: utf-8 -*-
"""文档上传（知识库管理页对接；无鉴权——单机场景）"""
from fastapi import APIRouter, File, UploadFile

from ..ingestion import pipeline

router = APIRouter()

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50MB 上限（曾无限制，超大文件会整块读入内存）


@router.post("/api/rag/documents/upload")
async def upload(file: UploadFile = File(...), title: str = "", tags: str = ""):
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        return {"success": False,
                "message": f"文件超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限，请拆分后上传。",
                "data": None}
    result = pipeline.ingest(file.filename or "未命名", data, title, tags)
    if "error" in result:
        return {"success": False, "message": result["error"], "data": None}
    return {"success": True, "message": None,
            "data": {"documentId": result["documentId"],
                     "fileName": result["fileName"],
                     "fileSize": len(data)}}
