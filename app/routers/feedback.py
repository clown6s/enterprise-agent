# -*- coding: utf-8 -*-
"""
反馈端点（rag-admin 前端兼容）：
POST /api/rag/admin/feedback/adopt     {query, answer, source} → 采纳回流
POST /api/rag/admin/feedback/unhelpful {query, answer, correction?} → 负反馈沉淀用例
"""
from fastapi import APIRouter, Header

from .. import mysql_store
from ..learn import feedback

router = APIRouter()


def _auth(authorization: str | None) -> dict | None:
    token = (authorization or "").removeprefix("Bearer ")
    return mysql_store.resolve_token(token)


def _unauthorized():
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"success": False, "message": "未登录或会话已过期，请重新登录", "data": None},
        status_code=401,
    )


@router.post("/api/rag/admin/feedback/adopt")
def adopt(body: dict, authorization: str | None = Header(default=None)):
    user = _auth(authorization)
    if not user:
        return _unauthorized()
    result = feedback.adopt(
        body.get("query") or "", body.get("answer") or "",
        body.get("source") or "", user["username"])
    return {"success": True, "message": None, "data": result}


@router.post("/api/rag/admin/feedback/unhelpful")
def unhelpful(body: dict, authorization: str | None = Header(default=None)):
    user = _auth(authorization)
    if not user:
        return _unauthorized()
    result = feedback.record_unhelpful(
        body.get("query") or "", body.get("answer") or "",
        body.get("correction") or "", user["username"])
    return {"success": True, "message": None, "data": result}
