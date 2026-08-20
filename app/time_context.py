# -*- coding: utf-8 -*-
"""项目业务时间：统一 Asia/Shanghai（UTC+8）。"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    """返回上海本地 naive 时间，兼容 MySQL DATETIME 和现有日期比较。"""
    return datetime.now(BUSINESS_TZ).replace(tzinfo=None)


def now_aware() -> datetime:
    """返回带 Asia/Shanghai 时区信息的当前时间，用于日志/跨系统时间。"""
    return datetime.now(BUSINESS_TZ)


def utc_now() -> datetime:
    """返回 UTC aware 时间。"""
    return datetime.now(timezone.utc)
