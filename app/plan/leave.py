# -*- coding: utf-8 -*-
"""请假 SOP 定义（从 sop.py 抽出，统一走 ApprovalDef 组件化）。
新增业务 = 本文件加一个 ApprovalDef 实例，其余模块自动感知。"""
from .approvals import ApprovalDef, _check_name, _check_reason, _check_date, _build_sop
from . import sop as _sop
from datetime import datetime


def _v_leave(params, employees):
    if e := _check_name(params, employees):
        return e
    s, end = params.get("startTime") or "", params.get("endTime") or ""
    if not s or not end:
        return "校验未通过：缺少请假起止日期，请说明请哪天到哪天。"
    if not _check_date(s) or not _check_date(end):
        return "校验未通过：日期格式无法识别，请用「明天」「8月18日」这类表述。"
    if datetime.fromisoformat(end) < datetime.fromisoformat(s):
        return "校验未通过：结束日期早于开始日期。"
    days = params.get("days")
    try:
        days = float(days) if days not in (None, "") else (datetime.fromisoformat(end) - datetime.fromisoformat(s)).days + 1
    except (TypeError, ValueError):
        return "校验未通过：请假天数无法识别。"
    if days < 0.5 or days > 30:
        return f"校验未通过：请假天数 {days:g} 天超出范围（0.5~30 天）。"
    return _check_reason(params)


def _fdate_start(v):
    return datetime.fromisoformat(v).strftime("%Y-%m-%d 00:00:00")


def _fdate_end(v):
    return datetime.fromisoformat(v).strftime("%Y-%m-%d 23:59:59")


LEAVE_DEF = ApprovalDef(
    code="leave_action", name="请假申请", prefix="LEAVE",
    table="oa_leave_application", business_type="leave",
    keywords=["请假", "提交请假", "帮我请假", "帮我请", "申请年假", "请事假",
              "请病假", "请年假", "请调休", "请个假", "请一天假", "请两天假",
              "请三天假", "请半天假", "请假一天", "请假两天", "请假三天",
              "天假", "天事假", "天病假", "天年假", "天调休"],
    fields={
        "userName": ("user_name", str),
        "leaveType": ("leave_type", lambda v: int(v or 1)),
        "startTime": ("start_time", _fdate_start),
        "endTime": ("end_time", _fdate_end),
        "days": ("days", lambda v: float(v or 0)),
        "reason": ("reason", str),
    },
    extract_schema=('{{"userName": "员工姓名", "startTime": "yyyy-MM-dd", '
                    '"endTime": "yyyy-MM-dd", "days": 数字, "leaveType": 数字, "reason": "事由"}}'),
    extra_rules=("leaveType：1事假 2病假 3年假；没说类型默认 1\n"
                 "2. \"下午/上午/半天\"记 days=0.5；days 必须与 startTime/endTime 一致"),
    validate=_v_leave,
    receipt_detail=lambda p: f"{p.get('startTime', '')} 至 {p.get('endTime', '')}",
)

# 注册进 sop 引擎（与 approvals.py 底部同模式）
_sop.register(_build_sop(LEAVE_DEF))
