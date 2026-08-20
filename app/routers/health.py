# -*- coding: utf-8 -*-
"""健康端点（看门狗探活用）：进程能响应即 UP"""
from fastapi import APIRouter

from ..time_context import now as business_now

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {"status": "UP", "service": "enterprise-agent", "time": business_now().isoformat()}
