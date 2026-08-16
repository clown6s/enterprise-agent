# -*- coding: utf-8 -*-
"""健康端点（看门狗探活用）：进程能响应即 UP"""
from datetime import datetime

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {"status": "UP", "service": "enterprise-agent", "time": datetime.now().isoformat()}
