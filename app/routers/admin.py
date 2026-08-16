# -*- coding: utf-8 -*-
"""
自我优化看板端点（admin 鉴权）：
GET /api/rag/admin/usage            LLM 用量（字符/耗时/分模型）
GET /api/rag/admin/kb-health/gaps  知识库缺口报告（兜底聚类 + 补库建议）
GET /api/rag/admin/health/verify   校验层熔断状态
"""
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from .. import mysql_store
from ..learn import gap_discovery, usage
from ..service import agent

router = APIRouter()


def _auth(authorization: str | None) -> dict | None:
    token = (authorization or "").removeprefix("Bearer ")
    return mysql_store.resolve_token(token)


def _unauthorized():
    return JSONResponse(
        {"success": False, "message": "未登录或会话已过期，请重新登录", "data": None},
        status_code=401,
    )


@router.get("/api/rag/admin/usage")
def usage_view(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    return {"success": True, "message": None, "data": usage.snapshot()}


@router.get("/api/rag/admin/kb-health/gaps")
def gaps_view(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    return {"success": True, "message": None, "data": gap_discovery.report()}


@router.get("/api/rag/admin/health/verify")
def verify_health(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    return {"success": True, "message": None, "data": agent.verify_breaker_state()}
