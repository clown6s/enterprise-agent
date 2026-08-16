# -*- coding: utf-8 -*-
"""文档上传（知识库管理页对接；无鉴权——单机场景）"""
from fastapi import APIRouter, File, UploadFile

from ..ingestion import pipeline

router = APIRouter()


@router.post("/api/rag/documents/upload")
async def upload(file: UploadFile = File(...), title: str = "", tags: str = ""):
    data = await file.read()
    result = pipeline.ingest(file.filename or "未命名", data, title, tags)
    if "error" in result:
        return {"success": False, "message": result["error"], "data": None}
    return {"success": True, "message": None,
            "data": {"documentId": result["documentId"],
                     "fileName": result["fileName"],
                     "fileSize": len(data)}}
